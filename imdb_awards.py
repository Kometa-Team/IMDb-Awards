import json, os, random, sys, time
from datetime import datetime, UTC

if sys.version_info[0] != 3 or sys.version_info[1] < 11:
    print("Version Error: Version: %s.%s.%s incompatible please use Python 3.11+" % (sys.version_info[0], sys.version_info[1], sys.version_info[2]))
    sys.exit(0)

try:
    import requests
    from git import Repo
    from kometautils import KometaLogger, KometaArgs, YAML
except (ModuleNotFoundError, ImportError):
    print("Requirements Error: Requirements are not installed")
    sys.exit(0)

base_url = "https://www.imdb.com"
event_url = f"{base_url}/event"
graphql_url = "https://api.graphql.imdb.com/"
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
graphql_headers = {
    "Content-Type": "application/json",
    "x-imdb-client-name": "imdb-web-next",
    "Accept-Language": "en-US,en;q=0.5",
    "User-Agent": "Mozilla/5.0"
}
session = requests.Session()
valid = YAML(path=os.path.join(base_dir, "event_validation.yml"), create=True, start_empty=args["clean"])
if args["clean"]:
    valid.data = YAML.inline({})
    valid.data.fa.set_block_style()

event_query = """
query EventEditions($eventId: ID!) {
  nominationEvent(id: $eventId) {
    name { text }
    editions {
      id
      year
      instanceWithinYear
    }
  }
}
"""

edition_query = """
query EventEdition($editionId: ID!) {
  nominationEventEdition(id: $editionId) {
    awards {
      text
      nominationCategories(first: 250) {
        total
        edges {
          node {
            category { text }
            nominations(first: 500) {
              total
              edges {
                node {
                  isWinner
                  awardedEntities {
                    ... on AwardedTitles {
                      awardTitles { title { id } }
                    }
                    ... on AwardedNames {
                      secondaryAwardTitles { title { id } }
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


def _graphql_request(query, variables, context):
    sleep_time = 0 if args["no-sleep"] else 2
    logger.info(f"{context} GraphQL Request{f' [Sleep: {sleep_time}]' if sleep_time else ''}")
    for attempt in range(1, 4):
        try:
            response = session.post(
                graphql_url,
                headers=graphql_headers,
                json={"query": query, "variables": variables},
                timeout=30
            )
        except requests.RequestException as e:
            logger.warning(f"{context} request attempt {attempt} failed: {e}")
            if attempt < 3:
                time.sleep(2 * attempt)
                continue
            raise RuntimeError(f"IMDb GraphQL request failed for {context}: {e}") from e

        content_type = response.headers.get("Content-Type", "")
        if response.status_code == 429 or 500 <= response.status_code < 600:
            if attempt < 3:
                logger.warning(f"{context} returned HTTP {response.status_code}; retrying")
                time.sleep(2 * attempt)
                continue

        if response.status_code < 200 or response.status_code >= 300:
            snippet = response.text[:500].replace("\n", " ")
            raise RuntimeError(
                f"IMDb GraphQL HTTP {response.status_code} for {context} "
                f"(Content-Type: {content_type or 'missing'}): {snippet}"
            )
        if "json" not in content_type.lower():
            snippet = response.text[:500].replace("\n", " ")
            raise RuntimeError(
                f"IMDb GraphQL returned non-JSON content for {context} "
                f"(Content-Type: {content_type or 'missing'}): {snippet}"
            )
        try:
            payload = response.json()
        except requests.exceptions.JSONDecodeError as e:
            snippet = response.text[:500].replace("\n", " ")
            raise RuntimeError(f"IMDb GraphQL returned invalid JSON for {context}: {snippet}") from e
        if payload.get("errors"):
            messages = "; ".join(error.get("message", "Unknown GraphQL error") for error in payload["errors"])
            raise RuntimeError(f"IMDb GraphQL error for {context}: {messages}")
        if sleep_time:
            time.sleep(sleep_time)
        return payload.get("data", {})
    raise RuntimeError(f"IMDb GraphQL request failed for {context}")


def _get_event(event_id, context):
    data = _graphql_request(event_query, {"eventId": event_id}, context)
    event = data.get("nominationEvent")
    if not event:
        raise RuntimeError(f"IMDb GraphQL returned no event data for {event_id}")
    return event


def _get_edition(edition_id, context):
    data = _graphql_request(edition_query, {"editionId": edition_id}, context)
    edition = data.get("nominationEventEdition")
    if not edition:
        raise RuntimeError(f"IMDb GraphQL returned no edition data for {edition_id}")
    for award in edition.get("awards", []):
        categories = award.get("nominationCategories", {})
        if categories.get("total", 0) > len(categories.get("edges", [])):
            raise RuntimeError(f"IMDb GraphQL category result was truncated for {context}")
        for category_edge in categories.get("edges", []):
            nominations = category_edge.get("node", {}).get("nominations", {})
            if nominations.get("total", 0) > len(nominations.get("edges", [])):
                raise RuntimeError(f"IMDb GraphQL nomination result was truncated for {context}")
    return edition


titles = {}
for i, event_id in enumerate(original_event_ids, 1):
    event_yaml = YAML(path=os.path.join(base_dir, "events", f"{event_id}.yml"), create=True, start_empty=args["clean"])
    old_data = event_yaml.data
    event_yaml.data = YAML.inline({})
    event_yaml.data.fa.set_block_style()
    event_years = []
    edition_ids = {}

    try:
        event_data = _get_event(event_id, f"[Event {i}/{total_ids}]")
    except RuntimeError as e:
        logger.error(f"Skipping event {event_id} due to fetch error: {e}")
        titles[event_id] = event_id
        continue

    titles[event_id] = event_data.get("name", {}).get("text", event_id)
    for year_data in event_data.get("editions", []):
        extra_params = '' if year_data.get("instanceWithinYear", 1) == 1 else f"-{year_data.get('instanceWithinYear')}"
        event_year = f"{year_data.get('year')}{extra_params}"
        event_years.append(event_year)
        edition_ids[event_year] = year_data.get("id")
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
            try:
                edition = _get_edition(
                    edition_ids[event_year],
                    f"[Event {i}/{total_ids}] [Year {j}/{total_years}]"
                )
            except RuntimeError as e:
                logger.error(f"Skipping year {event_year} for event {event_id} due to fetch error: {e}")
                if event_year in old_data:
                    event_yaml[event_year] = old_data[event_year]
                    event_yaml.data.yaml_add_eol_comment(event_year_url, event_year)
                first = False
                continue

            for award in edition.get("awards", []):
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
