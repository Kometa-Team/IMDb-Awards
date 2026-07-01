import json, os, random, sys, time, re
from datetime import datetime, UTC

if sys.version_info[0] != 3 or sys.version_info[1] < 11:
    print("Version Error: Version: %s.%s.%s incompatible please use Python 3.11+" % (sys.version_info[0], sys.version_info[1], sys.version_info[2]))
    sys.exit(0)

try:
    import cloudscraper
    import requests
    from git import Repo
    from lxml import html
    from kometautils import KometaLogger, KometaArgs, YAML
except (ModuleNotFoundError, ImportError):
    print("Requirements Error: Requirements are not installed")
    sys.exit(0)

base_url = "https://www.imdb.com"
event_url = f"{base_url}/event"
event_git_url = "https://github.com/Kometa-Team/IMDb-Awards/blob/master/event_validation.yml"
options = [
    {"arg": "ns", "key": "no-sleep",     "env": "NO_SLEEP",     "type": "bool", "default": False, "help": "Run without random sleep timers between requests."},
    {"arg": "cl", "key": "clean",        "env": "CLEAN",        "type": "bool", "default": False, "help": "Run a completely clean run."},
    {"arg": "tr", "key": "trace",        "env": "TRACE",        "type": "bool", "default": False, "help": "Run with extra trace logs."},
    {"arg": "lr", "key": "log-requests", "env": "LOG_REQUESTS", "type": "bool", "default": False, "help": "Run with every request logged."}
]
script_name = "IMDb Awards"
base_dir = os.path.dirname(os.path.abspath(__file__))
args = KometaArgs("Kometa-Team/IMDb-Awards", base_dir, options, use_nightly=False)
logger = KometaLogger(script_name, "imdb_awards", os.path.join(base_dir, "logs"), is_trace=args["trace"], log_requests=args["log-requests"])
logger.screen_width = 160
logger.header(args, sub=True)
logger.separator("Validating Options", space=False, border=False)
logger.start()
event_ids = YAML(path=os.path.join(base_dir, "event_ids.yml"))
original_event_ids = list(set([ev for ev in event_ids["event_ids"]]))
original_event_ids.sort()
total_ids = len(original_event_ids)
os.makedirs(os.path.join(base_dir, "events"), exist_ok=True)
logger.info(f"{total_ids} Event IDs: {original_event_ids}")
header = {
    "Accept-Language": "en-US,en;q=0.5",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/113.0"
}
scraper = cloudscraper.create_scraper()
valid = YAML(path=os.path.join(base_dir, "event_validation.yml"), create=True, start_empty=args["clean"])
if args["clean"]:
    valid.data = YAML.inline({})
    valid.data.fa.set_block_style()


def _fetch_with_playwright(url, user_agent):
    """Try to fetch the URL with Playwright and return (html_content, next_data_str-or-None, cookies).
    Returns (None, None, None) on failure or if Playwright not available.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        logger.warning("Playwright Python package not available: %s" % e)
        return None, None, None

    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
                logger.info("Playwright: browser launched")
            except Exception as e:
                logger.warning("Playwright launch failed: %s" % e)
                logger.warning("If you recently installed Playwright, run: 'playwright install --with-deps' (or 'playwright install').")
                return None, None, None

            context = browser.new_context(user_agent=user_agent)
            page = context.new_page()

            # Basic console/network logging to help diagnostics (not too verbose)
            page.on("console", lambda msg: logger.debug(f"[playwright console] {msg.type}: {msg.text}"))
            page.on("requestfailed", lambda req: logger.debug(f"[playwright requestfailed] {req.url} -> {req.failure}"))
            page.on("response", lambda resp: logger.debug(f"[playwright response] {resp.status} {resp.url}"))

            try:
                page.goto(url, timeout=60000)
                try:
                    page.wait_for_load_state("networkidle", timeout=45000)
                except Exception:
                    # networkidle may time out; proceed anyway
                    logger.debug("Playwright: networkidle wait timed out (continuing)")
            except Exception as e:
                logger.warning(f"Playwright navigation failed for {url}: {e}")

            data_str = None
            # Try to read #__NEXT_DATA__ textContent
            try:
                data = page.eval_on_selector("#__NEXT_DATA__", "el => el.textContent")
                if data:
                    data_str = data
            except Exception:
                data_str = None

            # Fallback: read window.__NEXT_DATA__ if present
            if not data_str:
                try:
                    data = page.evaluate("() => (window.__NEXT_DATA__ !== undefined ? window.__NEXT_DATA__ : null)")
                    if data:
                        if isinstance(data, str):
                            data_str = data
                        else:
                            data_str = json.dumps(data)
                except Exception:
                    data_str = None

            # Grab rendered HTML and cookies before closing
            try:
                content = page.content()
            except Exception:
                content = None

            try:
                cookies = context.cookies()
            except Exception:
                cookies = []

            try:
                browser.close()
            except Exception:
                pass

            logger.info(f"Playwright: fetched page, __NEXT_DATA__ present: {bool(data_str)}, cookies: {len(cookies)}")
            return content, data_str, cookies
    except Exception as e:
        logger.warning(f"Unexpected Playwright error: {e}")
        return None, None, None


def _sync_cookies_into_scraper(cookies):
    """Sync Playwright cookies (list of dicts) into global scraper session cookies."""
    global scraper
    if not cookies:
        return 0
    synced = 0
    try:
        for ck in cookies:
            name = ck.get("name")
            value = ck.get("value")
            domain = ck.get("domain")
            path = ck.get("path", "/")
            # requests cookie jar set accepts domain/path kwargs
            try:
                scraper.cookies.set(name, value, domain=domain, path=path)
                synced += 1
            except Exception:
                # fallback: set without domain/path
                try:
                    scraper.cookies.set(name, value)
                    synced += 1
                except Exception:
                    logger.debug(f"Failed to set cookie {name} in scraper")
    except Exception as e:
        logger.warning(f"Failed syncing cookies into scraper: {e}")
    return synced


def _request(url, xpath=None, extra=None, page_props=False):
    global scraper
    sleep_time = 0 if args["no-sleep"] else random.randint(2, 6)
    logger.info(f"{f'{extra} ' if extra else ''}URL: {url}{f' [Sleep: {sleep_time}]' if sleep_time else ''}")

    attempts = 3
    response = None
    for attempt in range(1, attempts + 1):
        try:
            response = scraper.get(url, headers=header, timeout=15)
        except Exception as e:
            logger.warning(f"Request attempt {attempt} failed for {url}: {e}")
            if attempt < attempts:
                time.sleep(2 * attempt)
                continue
            raise

        # If blocked, try recreating the scraper once
        if response.status_code == 403:
            logger.warning(f"Received 403 for {url}, recreating scraper and retrying (attempt {attempt})")
            time.sleep(3)
            scraper = cloudscraper.create_scraper()
            if attempt < attempts:
                continue

        # Detect AWS WAF JS challenge or 202 challenge page
        text_snippet = response.content.decode("utf-8", errors="replace")[:4000].lower()
        is_aws_waf = ("awswafintegration" in text_snippet) or ("challenge.js" in text_snippet) or ("window.awswafintegration" in text_snippet)
        if response.status_code == 202 or is_aws_waf:
            logger.info(f"Detected JS challenge/WAF (status={response.status_code}) for {url}; attempting browser fallback")
            rendered_html, data_str, cookies = _fetch_with_playwright(url, header.get("User-Agent"))
            if rendered_html is None:
                # Playwright not available or failed (likely missing browser binaries)
                msg = ("JS challenge detected for %s (status=%s). Playwright fallback unavailable or failed. "
                       "Locally run: 'pip install playwright' and then 'playwright install --with-deps' (or 'playwright install'). "
                       "In CI, add a step to install Playwright browsers before running this script.") % (url, response.status_code)
                logger.error(msg)
                if attempt < attempts:
                    time.sleep(5 * attempt)
                    continue
                raise RuntimeError(msg)

            # Sync cookies into scraper so further requests reuse validated session
            synced = _sync_cookies_into_scraper(cookies)
            logger.info(f"Playwright: synced {synced} cookies into scraper session")

            # If Playwright returned window.__NEXT_DATA__ or #__NEXT_DATA__ text, parse it now
            if page_props and data_str:
                try:
                    obj = json.loads(data_str)
                    return obj["props"]["pageProps"]
                except Exception as e:
                    logger.error(f"Failed to parse __NEXT_DATA__ from Playwright-rendered page for {url}: {e}")
                    raise RuntimeError(f"Failed to parse __NEXT_DATA__ from Playwright-rendered page for {url}: {e}")

            # Parse the rendered HTML
            try:
                doc = html.fromstring(rendered_html.encode("utf-8"))
            except Exception as e:
                snippet = rendered_html[:1000]
                logger.error(f"Failed to parse Playwright-rendered HTML for {url}: {e}\nSnippet: {snippet}")
                raise

            if sleep_time:
                time.sleep(sleep_time)

            if page_props:
                try:
                    script_nodes = doc.xpath("//script[@id='__NEXT_DATA__']/text()")
                except Exception:
                    script_nodes = []
                data_str2 = script_nodes[0] if script_nodes else None
                if not data_str2:
                    m = re.search(r"window\.__NEXT_DATA__\s*=\s*({.*?});", rendered_html, re.S)
                    if m:
                        data_str2 = m.group(1)
                if not data_str2:
                    snippet = rendered_html[:2000]
                    logger.error(f"__NEXT_DATA__ not found in Playwright-rendered response for {url}. Response snippet: {snippet}")
                    raise RuntimeError(f"__NEXT_DATA__ not found in Playwright-rendered response for {url}")
                try:
                    obj = json.loads(data_str2)
                    return obj["props"]["pageProps"]
                except Exception as e:
                    logger.error(f"Failed to parse __NEXT_DATA__ JSON from Playwright-rendered page for {url}: {e}")
                    raise RuntimeError(f"Failed to parse __NEXT_DATA__ JSON for {url}: {e}")
            else:
                return doc.xpath(xpath) if xpath else doc

        # Retry on server errors
        if 500 <= response.status_code < 600 and attempt < attempts:
            logger.warning(f"Server error {response.status_code} for {url}, retrying (attempt {attempt})")
            time.sleep(2 * attempt)
            continue

        # Otherwise break and use the response
        break

    if response is None:
        raise RuntimeError(f"No response obtained for {url}")

    # Parse HTML (normal non-challenge path)
    try:
        doc = html.fromstring(response.content)
    except Exception as e:
        snippet = (response.content[:1000] if response.content else b"").decode("utf-8", errors="replace")
        logger.error(f"Failed to parse HTML for {url}: {e}\nSnippet: {snippet}")
        raise

    if sleep_time:
        time.sleep(sleep_time)

    if page_props:
        try:
            script_nodes = doc.xpath("//script[@id='__NEXT_DATA__']/text()")
        except Exception:
            script_nodes = []

        data_str = script_nodes[0] if script_nodes else None

        if not data_str:
            try:
                text = response.content.decode("utf-8", errors="replace")
                m = re.search(r"window\.__NEXT_DATA__\s*=\s*({.*?});", text, re.S)
                if m:
                    data_str = m.group(1)
            except Exception:
                data_str = None

        if not data_str:
            snippet = (response.content[:2000] if response.content else b"").decode("utf-8", errors="replace")
            logger.error(f"__NEXT_DATA__ not found for {url} (status={response.status_code}). Response snippet: {snippet}")
            raise RuntimeError(f"__NEXT_DATA__ not found in response for {url} (status={response.status_code})")

        try:
            obj = json.loads(data_str)
        except Exception as e:
            logger.error(f"Failed to parse __NEXT_DATA__ JSON for {url}: {e}")
            raise RuntimeError(f"Failed to parse __NEXT_DATA__ JSON for {url}: {e}")

        if "props" not in obj or "pageProps" not in obj["props"]:
            logger.error(f"__NEXT_DATA__ JSON missing expected keys for {url}. Keys: {list(obj.keys())}")
            raise RuntimeError(f"__NEXT_DATA__ JSON missing expected keys for {url}")

        return obj["props"]["pageProps"]

    return doc.xpath(xpath) if xpath else doc


titles = {}
for i, event_id in enumerate(original_event_ids, 1):
    event_yaml = YAML(path=os.path.join(base_dir, "events", f"{event_id}.yml"), create=True, start_empty=args["clean"])
    old_data = event_yaml.data
    event_yaml.data = YAML.inline({})
    event_yaml.data.fa.set_block_style()
    event_years = []

    # Robust: skip single event if fetch fails so other events can continue
    try:
        json_data = _request(f"{event_url}/{event_id}", extra=f"[Event {i}/{total_ids}]", page_props=True)
    except RuntimeError as e:
        logger.error(f"Skipping event {event_id} due to fetch error: {e}")
        # preserve any existing event YAML and continue to next event
        continue

    titles[event_id] = json_data.get("eventName", f"{event_id}")
    for year_data in json_data.get("historyEventEditions", []):
        extra_params = '' if year_data.get("instanceWithinYear", 1) == 1 else f"-{year_data.get('instanceWithinYear')}"
        event_years.append(f"{year_data.get('year')}{extra_params}")
    total_years = len(event_years)
    if event_id not in valid:
        valid[event_id] = YAML.inline({"years": YAML.inline([]), "awards": YAML.inline([]), "categories": YAML.inline([])})
        valid[event_id].fa.set_block_style()
        valid[event_id]["awards"].fa.set_block_style()
        valid[event_id]["categories"].fa.set_block_style()
        valid[event_id].yaml_add_eol_comment(f"Award Options: {titles[event_id]}", "awards")
        valid[event_id].yaml_add_eol_comment(f"Category Options: {titles[event_id]}", "categories")
    valid.data.yaml_add_eol_comment(f"{titles[event_id]} ({event_url}/{event_id})", event_id)
    first = True
    for j, event_year in enumerate(event_years, 1):
        event_year = str(event_year)
        event_year_url = f"{event_url}/{event_id}/{f'{event_year}/1' if '-' not in event_year else event_year.replace('-', '/')}/"
        if first or args["clean"] or event_year not in old_data:
            event_data = {}
            # If a year-level fetch fails, log and skip the year (don't abort entire run)
            try:
                year_props = _request(event_year_url, extra=f"[Event {i}/{total_ids}] [Year {j}/{total_years}]", page_props=True)
            except RuntimeError as e:
                logger.error(f"Skipping year {event_year} for event {event_id} due to fetch error: {e}")
                continue

            for award in year_props.get("edition", {}).get("awards", []):
                award_name = award.get("text", "").lower()
                award_data = {}
                for cat in award.get("nominationCategories", {}).get("edges", []):
                    node = cat.get("node", {})
                    cat_name = award_name if node.get("category") is None else node.get("category", {}).get("text", "").lower()
                    nominees = []
                    winners = []
                    for nom in node.get("nominations", {}).get("edges", []):
                        nnode = nom.get("node", {})
                        awarded = nnode.get("awardedEntities", {})
                        if "awardTitles" in awarded:
                            prop = "awardTitles"
                        elif "secondaryAwardTitles" in awarded and awarded.get("secondaryAwardTitles"):
                            prop = "secondaryAwardTitles"
                        else:
                            prop = None
                        if prop:
                            for award_title in awarded.get(prop, []):
                                imdb_id = award_title.get("title", {}).get("id")
                                if imdb_id:
                                    nominees.append(imdb_id)
                                    if nnode.get("isWinner"):
                                        winners.append(imdb_id)
                    nominees.sort()
                    winners.sort()
                    if nominees or winners:
                        if cat_name not in award_data:
                            award_data[cat_name] = {"nominee": YAML.inline([]), "winner": YAML.inline([])}
                        for n in nominees:
                            if n not in award_data[cat_name]["nominee"]:
                                award_data[cat_name]["nominee"].append(n)
                        for w in winners:
                            if w not in award_data[cat_name]["winner"]:
                                award_data[cat_name]["winner"].append(w)
                        if cat_name not in valid[event_id]["categories"]:
                            valid[event_id]["categories"].append(cat_name)
                if award_data:
                    event_data[award_name] = dict(sorted(award_data.items()))
                    if award_name not in valid[event_id]["awards"]:
                        valid[event_id]["awards"].append(award_name)
            first = False
            event_yaml[event_year] = event_data
            event_yaml.data.yaml_add_eol_comment(event_year_url, event_year)
            if event_data and event_year not in valid[event_id]["years"]:
                if args["clean"]:
                    valid[event_id]["years"].append(event_year)
                else:
                    valid[event_id]["years"].insert(0, event_year)
        else:
            event_yaml[event_year] = old_data[event_year]
            event_yaml.data.yaml_add_eol_comment(event_year_url, event_year)
    valid[event_id]["awards"].sort()
    valid[event_id]["categories"].sort()
    event_yaml.data.yaml_set_start_comment(titles[event_id])
    event_yaml.yaml.width = 4096
    event_yaml.save()
    filter_stats = {"awards": {}, "categories": {}}
    for ev_year, award_data in event_yaml.items():
        for award_filter, cat_data in award_data.items():
            if award_filter not in filter_stats["awards"]:
                filter_stats["awards"][award_filter] = []
            if ev_year not in filter_stats["awards"][award_filter]:
                filter_stats["awards"][award_filter].append(ev_year)
            for cat_filter in cat_data:
                if cat_filter not in filter_stats["categories"]:
                    filter_stats["categories"][cat_filter] = []
                if ev_year not in filter_stats["categories"][cat_filter]:
                    filter_stats["categories"][cat_filter].append(ev_year)
    rv_years = valid[event_id]["years"][::-1]
    for ft in ["awards", "categories"]:
        for j, f in enumerate(valid[event_id][ft]):
            years = []
            start = ""
            end = ""
            current = 0
            comment = "No Events Found"
            if f in filter_stats[ft]:
                for y in reversed(filter_stats[ft][f]):
                    pos = rv_years.index(y)
                    if not start:
                        start = y
                    elif current + 1 == pos:
                        end = y
                    elif start and end:
                        years.append(f"{start}-{end}")
                        start = y
                        end = ""
                    else:
                        years.append(start)
                        start = y
                    current = pos
                if start and end:
                    years.append(f"{start}-{end}")
                elif start:
                    years.append(start)
                fs = len(filter_stats[ft][f])
                comment = f"{fs} Event{'s' if fs > 1 else ''}: {', '.join(years)}"
            valid[event_id][ft].yaml_add_eol_comment(comment, j, 0)

valid.yaml.width = 200
valid.save()
valid = YAML(path=os.path.join(base_dir, "event_validation.yml"))

event_ids["event_ids"] = YAML.inline(original_event_ids)
event_ids["event_ids"].fa.set_block_style()
for i, ev in enumerate(event_ids["event_ids"]):
    event_ids["event_ids"].yaml_add_eol_comment(titles[ev], i, 0)

event_ids.save()

if [item.a_path for item in Repo(path=".").index.diff(None) if item.a_path.endswith(".yml")]:

    with open("README.md", "r", encoding="utf-8") as f:
        readme_data = f.readlines()
    readme_data = readme_data[:readme_data.index("## Events Available\n") + 2]

    readme_data[2] = f"Last generated at: {datetime.now(UTC).strftime('%B %d, %Y %H:%M')} UTC\n"

    for ev in original_event_ids:
        readme_data.append(f"* [{titles[ev]}]({event_url}/{ev}) ([{ev}]({event_git_url}#L{valid.data[ev].lc.line}))\n")
        readme_data.append(f"  * [Award Filters]({event_git_url}#L{valid.data[ev]['awards'].lc.line})\n")
        readme_data.append(f"  * [Category Filters]({event_git_url}#L{valid.data[ev]['categories'].lc.line})\n")

    with open("README.md", "w", encoding="utf-8") as f:
        f.writelines(readme_data)

logger.separator(f"{script_name} Finished\nTotal Runtime: {logger.runtime()}")