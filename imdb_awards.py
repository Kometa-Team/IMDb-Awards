import json, os, random, sys, time
from datetime import datetime, UTC

if sys.version_info[0] != 3 or sys.version_info[1] < 11:
    print("Version Error: Version: %s.%s.%s incompatible please use Python 3.11+" % (sys.version_info[0], sys.version_info[1], sys.version_info[2]))
    sys.exit(0)

try:
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
valid = YAML(path=os.path.join(base_dir, "event_validation.yml"), create=True, start_empty=args["clean"])
if args["clean"]:
    valid.data = YAML.inline({})
    valid.data.fa.set_block_style()

def _request(url, xpath=None, extra=None, page_props=False):
    sleep_time = 0 if args["no-sleep"] else random.randint(2, 6)
    logger.info(f"{f'{extra} ' if extra else ''}URL: {url}{f' [Sleep: {sleep_time}]' if sleep_time else ''}")
    response = html.fromstring(requests.get(url, headers=header).content)
    if sleep_time:
        time.sleep(sleep_time)
    if page_props:
        return json.loads(response.xpath("//script[@id='__NEXT_DATA__']/text()")[0])["props"]["pageProps"]
    return response.xpath(xpath) if xpath else response

titles = {}
for i, event_id in enumerate(original_event_ids, 1):
    event_yaml = YAML(path=os.path.join(base_dir, "events", f"{event_id}.yml"), create=True, start_empty=args["clean"])
    old_data = event_yaml.data
    event_yaml.data = YAML.inline({})
    event_yaml.data.fa.set_block_style()
    event_years = []
    json_data = _request(f"{event_url}/{event_id}", extra=f"[Event {i}/{total_ids}]", page_props=True)
    titles[event_id] = json_data["eventName"]
    for year_data in json_data["historyEventEditions"]:
        extra = '' if year_data["instanceWithinYear"] == 1 else f"-{year_data['instanceWithinYear']}"
        event_years.append(f"{year_data['year']}{extra}")
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
            for award in _request(event_year_url, extra=f"[Event {i}/{total_ids}] [Year {j}/{total_years}]", page_props=True)["edition"]["awards"]:
                award_name = award["text"].lower()
                award_data = {}
                for cat in award["nominationCategories"]["edges"]:
                    cat_name = award_name if cat["node"]["category"] is None else cat["node"]["category"]["text"].lower()
                    nominees = []
                    winners = []
                    for nom in cat["node"]["nominations"]["edges"]:
                        if "awardTitles" in nom["node"]["awardedEntities"]:
                            prop = "awardTitles"
                        elif "secondaryAwardTitles" in nom["node"]["awardedEntities"] and nom["node"]["awardedEntities"]["secondaryAwardTitles"]:
                            prop = "secondaryAwardTitles"
                        else:
                            prop = None
                        if prop:
                            for award_title in nom["node"]["awardedEntities"][prop]:
                                imdb_id = award_title["title"]["id"]
                                if imdb_id:
                                    nominees.append(imdb_id)
                                    if nom["node"]["isWinner"]:
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




