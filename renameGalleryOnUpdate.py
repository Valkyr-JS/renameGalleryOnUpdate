import difflib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import traceback
from datetime import datetime

import requests

try:
    import psutil  # pip install psutil
    MODULE_PSUTIL = True
except Exception:
    MODULE_PSUTIL = False

try:
    import unidecode  # pip install Unidecode
    MODULE_UNIDECODE = True
except Exception:
    MODULE_UNIDECODE = False


try:
    import renameGalleryOnUpdate_config as config
except Exception:
    import config
import log


DB_VERSION_FILE_REFACTOR = 32
DB_VERSION_SCENE_STUDIO_CODE = 38

DRY_RUN = config.dry_run
DRY_RUN_FILE = None

if config.log_file:
    DRY_RUN_FILE = os.path.join(os.path.dirname(config.log_file), "renameGalleryOnUpdate_dryrun.txt")

if DRY_RUN:
    if DRY_RUN_FILE and not config.dry_run_append:
        if os.path.exists(DRY_RUN_FILE):
            os.remove(DRY_RUN_FILE)
    log.LogInfo("Dry mode on")

START_TIME = time.time()
FRAGMENT = json.loads(sys.stdin.read())

FRAGMENT_SERVER = FRAGMENT["server_connection"]
PLUGIN_DIR = FRAGMENT_SERVER["PluginDir"]


PLUGIN_ARGS = FRAGMENT['args'].get("mode")

#log.LogDebug("{}".format(FRAGMENT))


def callGraphQL(query, variables=None):
    # Session cookie for authentication
    graphql_port = str(FRAGMENT_SERVER['Port'])
    graphql_scheme = FRAGMENT_SERVER['Scheme']
    graphql_cookies = {'session': FRAGMENT_SERVER['SessionCookie']['Value']}
    graphql_headers = {
        "Accept-Encoding": "gzip, deflate, br",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Connection": "keep-alive",
        "DNT": "1"
    }
    graphql_domain = FRAGMENT_SERVER['Host']
    if graphql_domain == "0.0.0.0":
        graphql_domain = "localhost"
    # Stash GraphQL endpoint
    graphql_url = f"{graphql_scheme}://{graphql_domain}:{graphql_port}/graphql"

    json = {'query': query}
    if variables is not None:
        json['variables'] = variables
    try:
        response = requests.post(graphql_url, json=json, headers=graphql_headers, cookies=graphql_cookies, timeout=20)
    except Exception as e:
        exit_plugin(err=f"[FATAL] Error with the graphql request {e}")
    if response.status_code == 200:
        result = response.json()
        if result.get("error"):
            for error in result["error"]["errors"]:
                raise Exception(f"GraphQL error: {error}")
            return None
        if result.get("data"):
            return result.get("data")
    elif response.status_code == 401:
        exit_plugin(err="HTTP Error 401, Unauthorised.")
    else:
        raise ConnectionError(f"GraphQL query failed: {response.status_code} - {response.content}")


def graphql_getGallery(gallery_id):
    query = """
    query FindGallery($id: ID!) {
        findGallery(id: $id) {
            ...GalleryData
        }
    }
    fragment GalleryData on Gallery {
        id
        title
        date
        rating100
        organized""" + FILE_QUERY + """
        studio {
            id
            name
            parent_studio {
                id
                name
            }
        }
        tags {
            id
            name
        }
        performers {
            id
            name
            gender
            favorite
            rating
            stash_ids{
                endpoint
                stash_id
            }
        }
    }
    """
    variables = {
        "id": gallery_id
    }
    result = callGraphQL(query, variables)
    return result.get('findGallery')


# used for bulk
def graphql_findGallery(perPage, direc="DESC") -> dict:
    query = """
    query FindGalleries($filter: FindFilterType) {
        findGalleries(filter: $filter) {
            count
            galleries {
                ...SlimGalleryData
            }
        }
    }
    fragment SlimGalleryData on Gallery {
        id
        title
        date
        rating100
        organized
    """ + FILE_QUERY + """
        studio {
            id
            name
            parent_studio {
                id
                name
            }
        }
        tags {
            id
            name
        }
        performers {
            id
            name
            gender
            favorite
            rating100
            stash_ids {
                endpoint
                stash_id
            }
        }
    }    
    """
    # ASC DESC
    variables = {'filter': {"direction": direc, "page": 1, "per_page": perPage, "sort": "updated_at"}}
    result = callGraphQL(query, variables)
    return result.get("findGalleries")


# used to find duplicate
def graphql_findGallerybyPath(path, modifier) -> dict:
    query = """
    query FindGalleries($filter: FindFilterType, $gallery_filter: GalleryFilterType) {
        findGalleries(filter: $filter, gallery_filter: $gallery_filter) {
            count
            galleries {
                id
                title
            }
        }
    }
    """
    # ASC DESC
    variables = {
        'filter': {
            "direction": "ASC",
            "page": 1,
            "per_page": 40,
            "sort": "updated_at"
        },
        "gallery_filter": {
            "path": {
                "modifier": modifier,
                "value": path
            }
        }
    }
    result = callGraphQL(query, variables)
    return result.get("findGalleries")



def graphql_getConfiguration():
    query = """
        query Configuration {
            configuration {
                general {
                    databasePath
                }
            }
        }
    """
    result = callGraphQL(query)
    return result.get('configuration')


def graphql_getStudio(studio_id):
    query = """
        query FindStudio($id:ID!) {
            findStudio(id: $id) {
                id
                name
                parent_studio {
                    id
                    name
                }
            }
        }
    """
    variables = {
        "id": studio_id
    }
    result = callGraphQL(query, variables)
    return result.get("findStudio")


def graphql_removeGalleriesTag(id_galleries: list, id_tags: list):
    query = """
    mutation BulkGalleryUpdate($input: BulkGalleryUpdateInput!) {
        bulkGalleryUpdate(input: $input) {
            id
        }
    }    
    """
    variables = {'input': {"ids": id_galleries, "tag_ids": {"ids": id_tags, "mode": "REMOVE"}}}
    result = callGraphQL(query, variables)
    return result


def graphql_getBuild():
    query = """
        {
            systemStatus {
                databaseSchema
            }
        }
    """
    result = callGraphQL(query)
    return result['systemStatus']['databaseSchema']


def find_diff_text(a: str, b: str):
    addi = minus = stay = ""
    minus_ = addi_ = 0
    for _, s in enumerate(difflib.ndiff(a, b)):
        if s[0] == ' ':
            stay += s[-1]
            minus += "*"
            addi += "*"
        elif s[0] == '-':
            minus += s[-1]
            minus_ += 1
        elif s[0] == '+':
            addi += s[-1]
            addi_ += 1
    if minus_ > 20 or addi_ > 20:
        log.LogDebug(f"Diff Checker: +{addi_}; -{minus_};")
        log.LogDebug(f"OLD: {a}")
        log.LogDebug(f"NEW: {b}")
    else:
        log.LogDebug(f"Original: {a}\n- Charac: {minus}\n+ Charac: {addi}\n  Result: {b}")
    return


def has_handle(fpath, all_result=False):
    lst = []
    for proc in psutil.process_iter():
        try:
            for item in proc.open_files():
                if fpath == item.path:
                    if all_result:
                        lst.append(proc)
                    else:
                        return proc
        except Exception:
            pass
    return lst


def config_edit(name: str, state: bool):
    found = 0
    try:
        with open(config.__file__, 'r', encoding='utf8') as file:
            config_lines = file.readlines()
        with open(config.__file__, 'w', encoding='utf8') as file_w:
            for line in config_lines:
                if len(line.split("=")) > 1:
                    if name == line.split("=")[0].strip():
                        file_w.write(f"{name} = {state}\n")
                        found += 1
                        continue
                file_w.write(line)
    except PermissionError as err:
        log.LogError(f"You don't have the permission to edit config.py ({err})")
    return found


def check_longpath(path: str):
    # Trying to prevent error with long paths for Win10
    # https://docs.microsoft.com/en-us/windows/win32/fileio/maximum-file-path-limitation?tabs=cmd
    if len(path) > 240 and not IGNORE_PATH_LENGTH:
        log.LogError(f"The path is too long ({len(path)} > 240). You can look at 'order_field'/'ignore_path_length' in config.")
        return 1


def get_template_filename(gallery: dict):
    template = None
    # Change by Studio
    if gallery.get("studio") and config.studio_templates:
        template_found = False
        current_studio = gallery.get("studio")
        if config.studio_templates.get(current_studio['name']):
            template = config.studio_templates[current_studio['name']]
            template_found = True
        # by first Parent found
        while current_studio.get("parent_studio") and not template_found:
            if config.studio_templates.get(current_studio.get("parent_studio").get("name")):
                template = config.studio_templates[current_studio['parent_studio']['name']]
                template_found = True
            current_studio = graphql_getStudio(current_studio.get("parent_studio")['id'])

    # Change by Tag
    tags = [x["name"] for x in gallery["tags"]]
    if gallery.get("tags") and config.tag_templates:
        for match, job in config.tag_templates.items():
            if match in tags:
                template = job
                break
    return template


def get_template_path(gallery: dict):
    template = {"destination": "", "option": [], "opt_details": {}}
    # Change by Path
    if config.p_path_templates:
        for match, job in config.p_path_templates.items():
            if match in gallery["path"]:
                template["destination"] = job
                break

    # Change by Studio
    if gallery.get("studio") and config.p_studio_templates:
        if config.p_studio_templates.get(gallery["studio"]["name"]):
            template["destination"] = config.p_studio_templates[scene["studio"]["name"]]
        # by Parent
        if gallery["studio"].get("parent_studio"):
            if config.p_studio_templates.get(gallery["studio"]["name"]):
                template["destination"] = config.p_studio_templates[gallery["studio"]["name"]]

    # Change by Tag
    tags = [x["name"] for x in gallery["tags"]]
    if gallery.get("tags") and config.p_tag_templates:
        for match, job in config.p_tag_templates.items():
            if match in tags:
                template["destination"] = job
                break

    if gallery.get("tags") and config.p_tag_option:
        for tag in gallery["tags"]:
            if config.p_tag_option.get(tag["name"]):
                opt = config.p_tag_option[tag["name"]]
                template["option"].extend(opt)
                if "clean_tag" in opt:
                    if template["opt_details"].get("clean_tag"):
                        template["opt_details"]["clean_tag"].append(tag["id"])
                    else:
                        template["opt_details"] = {"clean_tag": [tag["id"]]}
    if not gallery['organized'] and PATH_NON_ORGANIZED:
        template["destination"] = PATH_NON_ORGANIZED
    return template


def sort_performer(lst_use: list, lst_app=[]):
    for p in lst_use:
        lst_use[p].sort()
    for p in lst_use.values():
        for n in p:
            if n not in lst_app:
                lst_app.append(n)
    return lst_app


def sort_rating(d: dict):
    new_d = {}
    for i in sorted(d.keys(), reverse=True):
        new_d[i] = d[i]
    return new_d


def extract_info(gallery: dict, template: None):
    # Grabbing things from Stash
    gallery_information = {}

    gallery_information['current_path'] = str(gallery['files']['path'])
    # note: contain the dot (.mp4)
    gallery_information['file_extension'] = os.path.splitext(gallery_information['current_path'])[1]
    # note: basename contains the extension
    gallery_information['current_filename'] = os.path.basename(gallery_information['current_path'])
    gallery_information['current_directory'] = os.path.dirname(gallery_information['current_path'])

    if template.get("path"):
        if "^*" in template["path"]["destination"]:
            template["path"]["destination"] = template["path"]["destination"].replace("^*", gallery_information['current_directory'])
        gallery_information['template_split'] = os.path.normpath(template["path"]["destination"]).split(os.sep)
    gallery_information['current_path_split'] = os.path.normpath(gallery_information['current_path']).split(os.sep)

    if FILENAME_ASTITLE and not gallery.get("title"):
        gallery["title"] = gallery_information['current_filename']

    # Grab Title (without extension if present)
    if gallery.get("title"):
        # Removing extension if present in title
        gallery_information['title'] = re.sub(fr"{gallery_information['file_extension']}$", "", gallery['title'])
        if PREPOSITIONS_REMOVAL:
            for word in PREPOSITIONS_LIST:
                gallery_information['title'] = re.sub(fr"^{word}[\s_-]", "", gallery_information['title'])

    # Grab Date
    gallery_information['date'] = gallery.get("date")
    if gallery_information['date']:
        date_gallery = datetime.strptime(gallery_information['date'], r"%Y-%m-%d")
        gallery_information['date_format'] = datetime.strftime(date_gallery, config.date_format)

    # Grab Rating
    if gallery.get("rating100"):
        gallery_information['rating'] = RATING_FORMAT.format(gallery['rating100'])

    # Grab Performer
    gallery_information['performer_path'] = None
    if gallery.get("performers"):
        perf_list = []
        perf_list_stashid = []
        perf_rating = {"0": []}
        perf_favorite = {"yes": [], "no": []}
        for perf in gallery['performers']:
            if perf.get("gender"):
                if perf['gender'] in PERFORMER_IGNOREGENDER:
                    continue
            elif "UNDEFINED" in PERFORMER_IGNOREGENDER:
                continue
            # path related
            if template.get("path"):
                if "inverse_performer" in template["path"]["option"]:
                    perf["name"] = re.sub(r"([a-zA-Z]+)(\s)([a-zA-Z]+)", r"\3 \1", perf["name"])
            perf_list.append(perf['name'])
            if perf.get('rating'):
                if perf_rating.get(str(perf['rating'])) is None:
                    perf_rating[str(perf['rating'])] = []
                perf_rating[str(perf['rating'])].append(perf['name'])
            else:
                perf_rating["0"].append(perf['name'])
            if perf.get('favorite'):
                perf_favorite['yes'].append(perf['name'])
            else:
                perf_favorite['no'].append(perf['name'])
            # if the path already contains the name we keep this one
            if perf["name"] in gallery_information['current_path_split'] and gallery_information.get('performer_path') is None and PATH_KEEP_ALRPERF:
                gallery_information['performer_path'] = perf["name"]
                log.LogDebug(f"[PATH] Keeping the current name of the performer '{perf['name']}'")
        perf_rating = sort_rating(perf_rating)
        # sort performer
        if PERFORMER_SORT == "rating":
            # sort alpha
            perf_list = sort_performer(perf_rating)
        elif PERFORMER_SORT == "favorite":
            perf_list = sort_performer(perf_favorite)
        elif PERFORMER_SORT == "mix":
            perf_list = []
            for p in perf_favorite:
                perf_favorite[p].sort()
            for p in perf_favorite.get("yes"):
                perf_list.append(p)
            perf_list = sort_performer(perf_rating, perf_list)
        elif PERFORMER_SORT == "mixid":
            perf_list = []
            for p in perf_favorite.get("yes"):
                perf_list.append(p)
            for p in perf_rating.values():
                for n in p:
                    if n not in perf_list:
                        perf_list.append(n)
        elif PERFORMER_SORT == "name":
            perf_list.sort()
        if not gallery_information['performer_path'] and perf_list:
            gallery_information['performer_path'] = perf_list[0]
        if len(perf_list) > PERFORMER_LIMIT:
            if not PERFORMER_LIMIT_KEEP:
                log.LogInfo(f"More than {PERFORMER_LIMIT} performer(s). Ignoring $performer")
                perf_list = []
            else:
                log.LogInfo(f"Limited the amount of performer to {PERFORMER_LIMIT}")
                perf_list = perf_list[0: PERFORMER_LIMIT]
        gallery_information['performer'] = PERFORMER_SPLITCHAR.join(perf_list)
        if perf_list:
            for p in perf_list:
                for perf in gallery['performers']:
                    #todo support other db that stashdb ?
                    if p == perf['name'] and perf.get('stash_ids'):
                        perf_list_stashid.append(perf['stash_ids'][0]["stash_id"])
                        break
            gallery_information['stashid_performer'] = PERFORMER_SPLITCHAR.join(perf_list_stashid)
        if not PATH_ONEPERFORMER:
            gallery_information['performer_path'] = PERFORMER_SPLITCHAR.join(perf_list)
    elif PATH_NOPERFORMER_FOLDER:
        gallery_information['performer_path'] = "NoPerformer"

    # Grab Studio name
    if gallery.get("studio"):
        if SQUEEZE_STUDIO_NAMES:
            gallery_information['studio'] = gallery['studio']['name'].replace(' ', '')
        else:
            gallery_information['studio'] = gallery['studio']['name']
        gallery_information['studio_family'] = gallery_information['studio']
        studio_hierarchy = [gallery_information['studio']]
        # Grab Parent name
        if gallery['studio'].get("parent_studio"):
            if SQUEEZE_STUDIO_NAMES:
                gallery_information['parent_studio'] = gallery['studio']['parent_studio']['name'].replace(' ', '')
            else:
                gallery_information['parent_studio'] = gallery['studio']['parent_studio']['name']
            gallery_information['studio_family'] = gallery_information['parent_studio']

            studio_p = gallery['studio']
            while studio_p.get("parent_studio"):
                studio_p = graphql_getStudio(studio_p['parent_studio']['id'])
                if studio_p:
                    if SQUEEZE_STUDIO_NAMES:
                        studio_hierarchy.append(studio_p['name'].replace(' ', ''))
                    else:
                        studio_hierarchy.append(studio_p['name'])
            studio_hierarchy.reverse()
        gallery_information['studio_hierarchy'] = studio_hierarchy
    # Grab Tags
    if gallery.get("tags"):
        tag_list = []
        for tag in gallery['tags']:
            # ignore tag in blacklist
            if tag['name'] in TAGS_BLACKLIST:
                continue
            # check if there is a whilelist
            if len(TAGS_WHITELIST) > 0:
                if tag['name'] in TAGS_WHITELIST:
                    tag_list.append(tag['name'])
            else:
                tag_list.append(tag['name'])
        gallery_information['tags'] = TAGS_SPLITCHAR.join(tag_list)

    if FIELD_WHITESPACE_SEP:
        for key, value in gallery_information.items():
            if key in ["current_path", "current_filename", "current_directory", "current_path_split", "template_split"]:
                continue
            if type(value) is str:
                gallery_information[key] = value.replace(" ", FIELD_WHITESPACE_SEP)
            elif type(value) is list:
                gallery_information[key] = [x.replace(" ", FIELD_WHITESPACE_SEP) for x in value]
    return gallery_information


def replace_text(text: str):
    for old, new in FILENAME_REPLACEWORDS.items():
        if type(new) is str:
            new = [new]
        if len(new) > 1:
            if new[1] == "regex":
                tmp = re.sub(old, new[0], text)
                if tmp != text:
                    log.LogDebug(f"Regex matched: {text} -> {tmp}")
            else:
                if new[1] == "word":
                    tmp = re.sub(fr'([\s_-])({old})([\s_-])', f'\\1{new[0]}\\3', text)
                elif new[1] == "any":
                    tmp = text.replace(old, new[0])
                if tmp != text:
                    log.LogDebug(f"'{old}' changed with '{new[0]}'")
        else:
            tmp = re.sub(fr'([\s_-])({old})([\s_-])', f'\\1{new[0]}\\3', text)
            if tmp != text:
                log.LogDebug(f"'{old}' changed with '{new[0]}'")
        text = tmp
    return tmp


def cleanup_text(text: str):
    text = re.sub(r'\(\W*\)|\[\W*\]|{[^a-zA-Z0-9]*}', '', text)
    text = re.sub(r'[{}]', '', text)
    text = remove_consecutive_nonword(text)
    return text.strip(" -_.")


def remove_consecutive_nonword(text: str):
    for _ in range(0, 10):
        m = re.findall(r'(\W+)\1+', text)
        if m:
            text = re.sub(r'(\W+)\1+', r'\1', text)
        else:
            break
    return text


def field_replacer(text: str, gallery_information:dict):
    field_found = re.findall(r"\$\w+", text)
    result = text
    title = None
    replaced_word = ""
    if field_found:
        field_found.sort(key=len, reverse=True)
    for i in range(0, len(field_found)):
        f = field_found[i].replace("$", "").strip("_")
        # If $performer is before $title, prevent having duplicate text.
        if f == "performer" and len(field_found) > i + 1 and gallery_information.get('performer'):
            if field_found[i+1] == "$title" and gallery_information.get('title') and PREVENT_TITLE_PERF:
                if re.search(f"^{gallery_information['performer'].lower()}", gallery_information['title'].lower()):
                    log.LogDebug("Ignoring the performer field because it's already in start of title")
                    result = result.replace("$performer", "")
                    continue
        replaced_word = gallery_information.get(f)
        if not replaced_word:
            replaced_word = ""
        if FIELD_REPLACER.get(f"${f}"):
            replaced_word = replaced_word.replace(FIELD_REPLACER[f"${f}"]["replace"], FIELD_REPLACER[f"${f}"]["with"])
        if f == "title":
            title = replaced_word.strip()
            continue
        if replaced_word == "":
            result = result.replace(field_found[i], replaced_word)
        else:
            result = result.replace(f"${f}", replaced_word)
    return result, title


def makeFilename(gallery_information: dict, query: str) -> str:
    new_filename = str(query)
    r, t = field_replacer(new_filename, gallery_information)
    if FILENAME_REPLACEWORDS:
        r = replace_text(r)
    if not t:
        r = r.replace("$title", "")
    r = cleanup_text(r)
    if t:
        r = r.replace("$title", t)
    # Replace spaces with splitchar
    r = r.replace(' ', FILENAME_SPLITCHAR)
    return r


def makePath(gallery_information: dict, query: str) -> str:
    new_filename = str(query)
    new_filename = new_filename.replace("$performer", "$performer_path")
    r, t = field_replacer(new_filename, gallery_information)
    if not t:
        r = r.replace("$title", "")
    r = cleanup_text(r)
    if t:
        r = r.replace("$title", t)
    return r


def capitalizeWords(s: str):
    # thanks to BCFC_1982 for it
    return re.sub(r"[A-Za-z]+('[A-Za-z]+)?", lambda word: word.group(0).capitalize(), s)


def create_new_filename(gallery_information: dict, template: str):
    new_filename = makeFilename(gallery_information, template) + DUPLICATE_SUFFIX[gallery_information['file_index']] + gallery_information['file_extension']
    if FILENAME_LOWER:
        new_filename = new_filename.lower()
    if FILENAME_TITLECASE:
        new_filename = capitalizeWords(new_filename)
    # Remove illegal character for Windows
    new_filename = re.sub('[\\/:"*?<>|]+', '', new_filename)

    if FILENAME_REMOVECHARACTER:
        new_filename = re.sub(f'[{FILENAME_REMOVECHARACTER}]+', '', new_filename)

    # Trying to remove non standard character
    if MODULE_UNIDECODE and UNICODE_USE:
        new_filename = unidecode.unidecode(new_filename, errors='preserve')
    else:
        # Using typewriter for Apostrophe
        new_filename = re.sub("[’‘”“]+", "'", new_filename)
    return new_filename


def remove_consecutive(liste: list):
    new_list = []
    for i in range(0, len(liste)):
        if i != 0 and liste[i] == liste[i - 1]:
            continue
        new_list.append(liste[i])
    return new_list


def create_new_path(gallery_information: dict, template: dict):
    # Create the new path
    # Split the template path
    path_split = gallery_information['template_split']
    path_list = []
    for part in path_split:
        if ":" in part and path_split[0]:
            path_list.append(part)
        elif part == "$studio_hierarchy":
            if not gallery_information.get("studio_hierarchy"):
                continue
            for p in gallery_information["studio_hierarchy"]:
                path_list.append(re.sub('[\\/:"*?<>|]+', '', p).strip())
        else:
            path_list.append(re.sub('[\\/:"*?<>|]+', '', makePath(gallery_information, part)).strip())
    # Remove blank, empty string
    path_split = [x for x in path_list if x]
    # The first character was a seperator, so put it back.
    if path_list[0] == "":
        path_split.insert(0, "")

    if PREVENT_CONSECUTIVE:
        # remove consecutive (/FolderName/FolderName/gallery.zip -> FolderName/gallery.zip
        path_split = remove_consecutive(path_split)

    if "^*" in template["path"]["destination"]:
        if gallery_information['current_directory'] != os.sep.join(path_split):
            path_split.pop(len(gallery_information['current_directory']))

    path_edited = os.sep.join(path_split)

    if FILENAME_REMOVECHARACTER:
        path_edited = re.sub(f'[{FILENAME_REMOVECHARACTER}]+', '', path_edited)

    # Using typewriter for Apostrophe
    new_path = re.sub("[’‘”“]+", "'", path_edited)

    return new_path


def connect_db(path: str):
    try:
        sqliteConnection = sqlite3.connect(path, timeout=10)
        log.LogDebug("Python successfully connected to SQLite")
    except sqlite3.Error as error:
        log.LogError(f"FATAL SQLITE Error: {error}")
        return None
    return sqliteConnection


def checking_duplicate_db(gallery_information: dict):
    galleries = graphql_findGallerybyPath(gallery_information['final_path'], "EQUALS")
    if galleries["count"] > 0:
        log.LogError("Duplicate path detected")
        for dupl_row in galleries["galleries"]:
            log.LogWarning(f"Identical path: [{dupl_row['id']}]")
        return 1
    galleries = graphql_findGallerybyPath(gallery_information['new_filename'], "EQUALS")
    if galleries["count"] > 0:
        for dupl_row in galleries["galleries"]:
            if dupl_row['id'] != gallery_information['gallery_id']:
                log.LogWarning(f"Duplicate filename: [{dupl_row['id']}]")


def db_rename(stash_db: sqlite3.Connection, gallery_information):
    cursor = stash_db.cursor()
    # Database rename
    cursor.execute("UPDATE galleries SET path=? WHERE id=?;", [gallery_information['final_path'], gallery_information['gallery_id']])
    stash_db.commit()
    # Close DB
    cursor.close()


def db_rename_refactor(stash_db: sqlite3.Connection, gallery_information):
    cursor = stash_db.cursor()
    # 2022-09-17T11:25:52+02:00
    mod_time = datetime.now().astimezone().isoformat('T', 'seconds')

    # get the next id that we should use if needed
    cursor.execute("SELECT MAX(id) from folders")
    new_id = cursor.fetchall()[0][0] + 1

    # get the old folder id
    cursor.execute("SELECT id FROM folders WHERE path=?", [gallery_information['current_directory']])
    old_folder_id = cursor.fetchall()[0][0]

    # check if the folder of file is created in db
    cursor.execute("SELECT id FROM folders WHERE path=?", [gallery_information['new_directory']])
    folder_id = cursor.fetchall()
    if not folder_id:
        dir = gallery_information['new_directory']
        # reduce the path to find a parent folder
        for _ in range(1, len(gallery_information['new_directory'].split(os.sep))):
            dir = os.path.dirname(dir)
            cursor.execute("SELECT id FROM folders WHERE path=?", [dir])
            parent_id = cursor.fetchall()
            if parent_id:
                # create a new row with the new folder with the parent folder find above
                cursor.execute(
                    "INSERT INTO 'main'.'folders'('id', 'path', 'parent_folder_id', 'mod_time', 'created_at', 'updated_at', 'zip_file_id') VALUES (?, ?, ?, ?, ?, ?, ?);",
                    [
                        new_id, gallery_information['new_directory'], parent_id[0][0],
                        mod_time, mod_time, mod_time, None
                    ])
                stash_db.commit()
                folder_id = new_id
                break
    else:
        folder_id = folder_id[0][0]
    if folder_id:
        cursor.execute("SELECT file_id from gallery_files WHERE gallery_id=?", [gallery_information['gallery_id']])
        file_ids = cursor.fetchall()
        file_id = None
        for f in file_ids:
            # it can have multiple file for a scene
            cursor.execute("SELECT parent_folder_id from files WHERE id=?", [f[0]])
            check_parent = cursor.fetchall()[0][0]
            # if the parent id is the one found above section, we find our file.s
            if check_parent == old_folder_id:
                file_id = f[0]
                break
        if file_id:
            #log.LogDebug(f"UPDATE files SET basename={gallery_information['new_filename']}, parent_folder_id={folder_id}, updated_at={mod_time} WHERE id={file_id};")
            cursor.execute("UPDATE files SET basename=?, parent_folder_id=?, updated_at=? WHERE id=?;", [gallery_information['new_filename'], folder_id, mod_time, file_id])
            cursor.close()
            stash_db.commit()
        else:
            raise Exception("Failed to find file_id")
    else:
        cursor.close()
        raise Exception(f"You need to setup a library with the new location ({gallery_information['new_directory']}) and scan at least 1 file")


def file_rename(current_path: str, new_path: str, gallery_information: dict):
    # OS Rename
    if not os.path.isfile(current_path):
        log.LogWarning(f"[OS] File doesn't exist in your Disk/Drive ({current_path})")
        return 1
    # moving/renaming
    new_dir = os.path.dirname(new_path)
    current_dir = os.path.dirname(current_path)
    if not os.path.exists(new_dir):
        log.LogInfo(f"Creating folder because it don't exist ({new_dir})")
        os.makedirs(new_dir)
    try:
        shutil.move(current_path, new_path)
    except PermissionError as err:
        if "[WinError 32]" in str(err) and MODULE_PSUTIL:
            log.LogWarning("A process is using this file (Probably FFMPEG), trying to find it ...")
            # Find which process accesses the file, it's ffmpeg for sure...
            process_use = has_handle(current_path, PROCESS_ALLRESULT)
            if process_use:
                # Terminate the process then try again to rename
                log.LogDebug(f"Process that uses this file: {process_use}")
                if PROCESS_KILL:
                    p = psutil.Process(process_use.pid)
                    p.terminate()
                    p.wait(10)
                    # If process is not terminated, this will create an error again.
                    try:
                        shutil.move(current_path, new_path)
                    except Exception as err:
                        log.LogError(f"Something still prevents renaming the file. {err}")
                        return 1
                else:
                    log.LogError("A process prevents renaming the file.")
                    return 1
        else:
            log.LogError(f"Something prevents renaming the file. {err}")
            return 1
    # checking if the move/rename work correctly
    if os.path.isfile(new_path):
        log.LogInfo(f"[OS] File Renamed! ({current_path} -> {new_path})")
        if LOGFILE:
            try:
                with open(LOGFILE, 'a', encoding='utf-8') as f:
                    f.write(f"{gallery_information['gallery_id']}|{current_path}|{new_path}\n")
            except Exception as err:
                shutil.move(new_path, current_path)
                log.LogError(f"Restoring the original path, error writing the logfile: {err}")
                return 1
        if REMOVE_EMPTY_FOLDER:
            with os.scandir(current_dir) as it:
                if not any(it):
                    log.LogInfo(f"Removing empty folder ({current_dir})")
                    try:
                        os.rmdir(current_dir)
                    except Exception as err:
                        log.logWarning(f"Fail to delete empty folder {current_dir} - {err}")
    else:
        # I don't think it's possible.
        log.LogError(f"[OS] Failed to rename the file ? {new_path}")
        return 1

def associated_rename(gallery_information: dict):
    if ASSOCIATED_EXT:
        for ext in ASSOCIATED_EXT:
            p = os.path.splitext(gallery_information['current_path'])[0] + "." + ext
            p_new = os.path.splitext(gallery_information['final_path'])[0] + "." + ext
            if os.path.isfile(p):
                try:
                    shutil.move(p, p_new)
                except Exception as err:
                    log.LogError(f"Something prevents renaming this file '{p}' - err: {err}")
                    continue
            if os.path.isfile(p_new):
                log.LogInfo(f"[OS] Associate file renamed ({p_new})")
                if LOGFILE:
                    try:
                        with open(LOGFILE, 'a', encoding='utf-8') as f:
                            f.write(f"{gallery_information['gallery_id']}|{p}|{p_new}\n")
                    except Exception as err:
                        shutil.move(p_new, p)
                        log.LogError(f"Restoring the original name, error writing the logfile: {err}")


def renamer(gallery_id, db_conn=None):
    option_dryrun = False
    if type(gallery_id) is dict:
        stash_gallery = gallery_id
        gallery_id = stash_gallery['id']
    elif type(gallery_id) is int:
        stash_gallery = graphql_getGallery(gallery_id)

    if config.only_organized and not stash_gallery['organized'] and not PATH_NON_ORGANIZED:
        log.LogDebug(f"[{gallery_id}] Gallery ignored (not organized)")
        return

    # refractor file support
    if stash_gallery.get("path"):
        stash_gallery["file"]["path"] = stash_gallery["path"]
        gallery_files = [stash_gallery["file"]]
        del stash_gallery["path"]
        del stash_gallery["file"]
    elif stash_gallery.get("files"):
        gallery_files = stash_gallery["files"]
        del stash_gallery["files"]
    else:
        gallery_files = []
    stash_db = None
    for i in range(0, len(gallery_files)):
        gallery_file = gallery_files[i]
        # refractor file support
        stash_gallery["path"] = gallery_file["path"]
        stash_gallery["file"] = gallery_file

        # Tags > Studios > Default
        template = {}
        template["filename"] = get_template_filename(stash_gallery)
        template["path"] = get_template_path(stash_gallery)
        if not template["path"].get("destination"):
            if config.p_use_default_template:
                log.LogDebug("[PATH] Using default template")
                template["path"] = {"destination": config.p_default_template, "option": [], "opt_details": {}}
            else:
                template["path"] = None
        else:
            if template["path"].get("option"):
                if "dry_run" in template["path"]["option"] and not DRY_RUN:
                    log.LogInfo("Dry-Run on (activate by option)")
                    option_dryrun = True
        if not template["filename"] and config.use_default_template:
            log.LogDebug("[FILENAME] Using default template")
            template["filename"] = config.default_template

        if not template["filename"] and not template["path"]:
            log.LogWarning(f"[{gallery_id}] No template for this gallery.")
            return

        #log.LogDebug("Using this template: {}".format(filename_template))
        gallery_information = extract_info(stash_gallery, template)
        log.LogDebug(f"[{gallery_id}] Gallery information: {gallery_information}")
        log.LogDebug(f"[{gallery_id}] Template: {template}")

        gallery_information['gallery_id'] = gallery_id
        gallery_information['file_index'] = i

        for removed_field in ORDER_SHORTFIELD:
            if removed_field:
                if gallery_information.get(removed_field.replace("$", "")):
                    del gallery_information[removed_field.replace("$", "")]
                    log.LogWarning(f"removed {removed_field} to reduce the length path")
                else:
                    continue
            if template["filename"]:
                gallery_information['new_filename'] = create_new_filename(gallery_information, template["filename"])
            else:
                gallery_information['new_filename'] = gallery_information['current_filename']
            if template.get("path"):
                gallery_information['new_directory'] = create_new_path(gallery_information, template)
            else:
                gallery_information['new_directory'] = gallery_information['current_directory']
            gallery_information['final_path'] = os.path.join(gallery_information['new_directory'], gallery_information['new_filename'])
            # check length of path
            if IGNORE_PATH_LENGTH or len(gallery_information['final_path']) <= 240:
                break

        if check_longpath(gallery_information['final_path']):
            if (DRY_RUN or option_dryrun) and LOGFILE:
                with open(DRY_RUN_FILE, 'a', encoding='utf-8') as f:
                    f.write(f"[LENGTH LIMIT] {gallery_information['gallery_id']}|{gallery_information['final_path']}\n")
            continue

        #log.LogDebug(f"Filename: {gallery_information['current_filename']} -> {gallery_information['new_filename']}")
        #log.LogDebug(f"Path: {gallery_information['current_directory']} -> {gallery_information['new_directory']}")

        if gallery_information['final_path'] == gallery_information['current_path']:
            log.LogInfo(f"Everything is ok. ({gallery_information['current_filename']})")
            continue

        if gallery_information['current_directory'] != gallery_information['new_directory']:
            log.LogInfo("File will be moved to another directory")
            log.LogDebug(f"[OLD path] {gallery_information['current_path']}")
            log.LogDebug(f"[NEW path] {gallery_information['final_path']}")

        if gallery_information['current_filename'] != gallery_information['new_filename']:
            log.LogInfo("The filename will be changed")
            if ALT_DIFF_DISPLAY:
                find_diff_text(gallery_information['current_filename'], gallery_information['new_filename'])
            else:
                log.LogDebug(f"[OLD filename] {gallery_information['current_filename']}")
                log.LogDebug(f"[NEW filename] {gallery_information['new_filename']}")

        if (DRY_RUN or option_dryrun) and LOGFILE:
            with open(DRY_RUN_FILE, 'a', encoding='utf-8') as f:
                f.write(f"{gallery_information['gallery_id']}|{gallery_information['current_path']}|{gallery_information['final_path']}\n")
            continue
        # check if there is already a file where the new path is
        err = checking_duplicate_db(gallery_information)
        while err and gallery_information['file_index']<=len(DUPLICATE_SUFFIX):
            log.LogDebug("Duplicate filename detected, increasing file index")
            gallery_information['file_index'] = gallery_information['file_index'] + 1
            gallery_information['new_filename'] = create_new_filename(gallery_information, template["filename"])
            gallery_information['final_path'] = os.path.join(gallery_information['new_directory'], gallery_information['new_filename'])
            log.LogDebug(f"[NEW filename] {gallery_information['new_filename']}")
            log.LogDebug(f"[NEW path] {gallery_information['final_path']}")
            err = checking_duplicate_db(gallery_information)
        # abort
        if err:
            raise Exception("duplicate")
        # connect to the db
        if not db_conn:
            stash_db = connect_db(STASH_DATABASE)
            if stash_db is None:
                return
        else:
            stash_db = db_conn
        try:
            # rename file on your disk
            err = file_rename(gallery_information['current_path'], gallery_information['final_path'], gallery_information)
            if err:
                raise Exception("rename")
            # rename file on your db
            try:
                if DB_VERSION >= DB_VERSION_FILE_REFACTOR:
                    db_rename_refactor(stash_db, gallery_information)
                else:
                    db_rename(stash_db, gallery_information)
            except Exception as err:
                log.LogError(f"error when trying to update the database ({err}), revert the move...")
                err = file_rename(gallery_information['final_path'], gallery_information['current_path'], gallery_information)
                if err:
                    raise Exception("rename")
                raise Exception("database update")
            if i == 0:
                associated_rename(gallery_information)
            if template.get("path"):
                if "clean_tag" in template["path"]["option"]:
                    graphql_removeGalleriesTag([gallery_information['gallery_id']], template["path"]["opt_details"]["clean_tag"])
        except Exception as err:
            log.LogError(f"Error during database operation ({err})")
            if not db_conn:
                log.LogDebug("[SQLITE] Database closed")
                stash_db.close()
            continue
    if not db_conn and stash_db:
        stash_db.close()
        log.LogInfo("[SQLITE] Database updated and closed!")


def exit_plugin(msg=None, err=None):
    if msg is None and err is None:
        msg = "plugin ended"
    log.LogDebug("Execution time: {}s".format(round(time.time() - START_TIME, 5)))
    output_json = {"output": msg, "error": err}
    print(json.dumps(output_json))
    sys.exit()


if PLUGIN_ARGS:
    log.LogDebug("--Starting Plugin 'Renamer'--")
    if "bulk" not in PLUGIN_ARGS:
        if "enable" in PLUGIN_ARGS:
            log.LogInfo("Enable hook")
            success = config_edit("enable_hook", True)
        elif "disable" in PLUGIN_ARGS:
            log.LogInfo("Disable hook")
            success = config_edit("enable_hook", False)
        elif "dryrun" in PLUGIN_ARGS:
            if config.dry_run:
                log.LogInfo("Disable dryrun")
                success = config_edit("dry_run", False)
            else:
                log.LogInfo("Enable dryrun")
                success = config_edit("dry_run", True)
        if not success:
            log.LogError("Script failed to change the value")
        exit_plugin("script finished")
else:
    if not config.enable_hook:
        exit_plugin("Hook disabled")
    log.LogDebug("--Starting Hook 'Renamer'--")
    FRAGMENT_HOOK_TYPE = FRAGMENT["args"]["hookContext"]["type"]
    FRAGMENT_SCENE_ID = FRAGMENT["args"]["hookContext"]["id"]

LOGFILE = config.log_file

#Gallery.Update.Post
#if FRAGMENT_HOOK_TYPE == "Scene.Update.Post":


STASH_CONFIG = graphql_getConfiguration()
STASH_DATABASE = STASH_CONFIG['general']['databasePath']

# READING CONFIG

ASSOCIATED_EXT = config.associated_extension

FIELD_WHITESPACE_SEP = config.field_whitespaceSeperator
FIELD_REPLACER = config.field_replacer

FILENAME_ASTITLE = config.filename_as_title
FILENAME_LOWER = config.lowercase_Filename
FILENAME_TITLECASE = config.titlecase_Filename
FILENAME_SPLITCHAR = config.filename_splitchar
FILENAME_REMOVECHARACTER = config.removecharac_Filename
FILENAME_REPLACEWORDS = config.replace_words

PERFORMER_SPLITCHAR = config.performer_splitchar
PERFORMER_LIMIT = config.performer_limit
PERFORMER_LIMIT_KEEP = config.performer_limit_keep
PERFORMER_SORT = config.performer_sort
PERFORMER_IGNOREGENDER = config.performer_ignoreGender
PREVENT_TITLE_PERF = config.prevent_title_performer

DUPLICATE_SUFFIX = config.duplicate_suffix

PREPOSITIONS_LIST = config.prepositions_list
PREPOSITIONS_REMOVAL = config.prepositions_removal

SQUEEZE_STUDIO_NAMES = config.squeeze_studio_names

RATING_FORMAT = config.rating_format

TAGS_SPLITCHAR = config.tags_splitchar
TAGS_WHITELIST = config.tags_whitelist
TAGS_BLACKLIST = config.tags_blacklist

IGNORE_PATH_LENGTH = config.ignore_path_length

PREVENT_CONSECUTIVE = config.prevent_consecutive
REMOVE_EMPTY_FOLDER = config.remove_emptyfolder

PROCESS_KILL = config.process_kill_attach
PROCESS_ALLRESULT = config.process_getall
UNICODE_USE = config.use_ascii

ORDER_SHORTFIELD = config.order_field
ORDER_SHORTFIELD.insert(0, None)

ALT_DIFF_DISPLAY = config.alt_diff_display

PATH_NOPERFORMER_FOLDER = config.path_noperformer_folder
PATH_KEEP_ALRPERF = config.path_keep_alrperf
PATH_NON_ORGANIZED = config.p_non_organized
PATH_ONEPERFORMER = config.path_one_performer

DB_VERSION = graphql_getBuild()
if DB_VERSION >= DB_VERSION_FILE_REFACTOR:
    FILE_QUERY = """
            files {
                path
                video_codec
                audio_codec
                width
                height
                frame_rate
                duration
                bit_rate
                fingerprints {
                    type
                    value
                }
            }
    """
else:
    FILE_QUERY = """
            path
            file {
                video_codec
                audio_codec
                width
                height
                framerate
                bitrate
                duration
            }
    """
if DB_VERSION >= DB_VERSION_SCENE_STUDIO_CODE:
    FILE_QUERY = f"        code{FILE_QUERY}"

if PLUGIN_ARGS:
    if "bulk" in PLUGIN_ARGS:
        scenes = graphql_findGallery(config.batch_number_scene, "ASC")
        log.LogDebug(f"Count scenes: {len(scenes['scenes'])}")
        progress = 0
        progress_step = 1 / len(scenes['scenes'])
        stash_db = connect_db(STASH_DATABASE)
        if stash_db is None:
            exit_plugin()
        for scene in scenes['scenes']:
            log.LogDebug(f"** Checking scene: {scene['title']} - {scene['id']} **")
            try:
                renamer(scene, stash_db)
            except Exception as err:
                log.LogError(f"main function error: {err}")
            progress += progress_step
            log.LogProgress(progress)
        stash_db.close()
        log.LogInfo("[SQLITE] Database closed!")
else:
    try:
        renamer(FRAGMENT_SCENE_ID)
    except Exception as err:
        log.LogError(f"main function error: {err}")
        traceback.print_exc()

exit_plugin("Successful!")

