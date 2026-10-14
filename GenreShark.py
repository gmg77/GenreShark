"""
GenreShark - Auto-Consensus MP3 Genre Tagger
====================================================
A multi-source genre tagger for local MP3 libraries. Polls Discogs, Last.fm, iTunes,
TheAudioDB, Genius and MusicBrainz, lets each source vote with its top tags, and
normalizes sub-genres into clean Master Genres. A genre is only written when at least
two sources agree (or, for compilations / artist fallbacks, when a single source
returns a recognised Master Genre).

Requires Python 3.9+.

Dependencies:
    pip install requests musicbrainzngs mutagen
    pip install beautifulsoup4        # optional: only for Last.fm scraping / Genius

Usage:
    python GenreShark.py "D:\\Music"                  # tag a library
    python GenreShark.py "D:\\Music" --dry-run        # preview + CSV report, change nothing
    python GenreShark.py "D:\\Music" --only-untagged  # never touch existing genres
    python GenreShark.py "D:\\Music" --undo           # revert the most recent run
    python GenreShark.py "D:\\Music" --list-runs      # show runs that can be undone
    python GenreShark.py --selftest                   # offline sanity checks

Options:
    --overwrite / --no-overwrite   answer the overwrite question up front
    --reprocess                    ignore saved progress and rescan everything
    --services lastfm,itunes       use only some sources
    --report FILE.csv              write a CSV of every change (a dry run does this itself)
    --undo                         revert the most recent run (add --dry-run to preview)
    --undo-run RUN_ID|all          revert one specific run, or every run
    --force                        with --undo: restore even if the genre was edited since
    -v                             show request errors and other debug detail

Settings live in config.json next to this script (created as an empty template on first
run). A value in config.json wins; environment variables are the fallback; empty values
never win. Keep config.json out of git and cloud-synced folders.
    CONTACT_EMAIL, DISCOGS_TOKEN, GENIUS_CLIENT_ID, GENIUS_CLIENT_SECRET, LASTFM_API_KEY
Set LASTFM_API_KEY to use the official Last.fm API instead of scraping the website.

After you edit the genre taxonomy, bump TAXONOMY_VERSION so finished folders and cached
artists are re-evaluated with the new rules.

Files written to the music folder:
    tagger_state.jsonl     per-folder results (so finished folders are skipped)
    artist_cache.json      artist -> genre cache that survives between runs (versioned)
    failed_searches.txt    human-readable list of folders with no result
    genre_undo.jsonl       every tag change, with the previous genre, for --undo
    genre_dry_run_*.csv    the report a dry run writes (the only thing a dry run writes)
"""

import argparse
import concurrent.futures
import csv
import json
import logging
import os
import re
import sys
import threading
import time
import unicodedata
import urllib.parse
from collections import Counter
from datetime import datetime
from difflib import SequenceMatcher

# --- DEPENDENCY CHECK ---
try:
    import requests
    import musicbrainzngs
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    from mutagen.easyid3 import EasyID3
    from mutagen.id3 import ID3, TCON, ID3NoHeaderError
except ImportError as e:
    module_name = (getattr(e, "name", None) or str(e)).split(".")[0]
    print("=" * 60)
    print(f" [!] MISSING REQUIRED MODULE: {module_name}")
    print(f"     Please run: pip install {module_name}")
    sys.exit(1)

try:  # optional: only needed for Last.fm scraping (no API key) and Genius
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

# Enable ANSI escape codes for Windows terminals
if sys.platform == 'win32':
    os.system('')

log = logging.getLogger("genretagger")

# ==========================================
# CONFIGURATION & API KEYS (config.json, then environment)
# ==========================================
__version__ = "2.0.0"
USER_AGENT = f"GenreShark/{__version__}"

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
CONFIG_KEYS = ("CONTACT_EMAIL", "DISCOGS_TOKEN", "GENIUS_CLIENT_ID",
               "GENIUS_CLIENT_SECRET", "LASTFM_API_KEY")


def load_config(path=CONFIG_PATH):
    """Read config.json (read-only; a missing or broken file just yields no settings)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("Could not read %s: %s", path, exc)
        return {}


def _pick(key, cfg, env, default=""):
    """config.json wins, then the environment, then the default. Empty values never win,
    so a blank template entry can't hide a real environment variable."""
    return str(cfg.get(key) or env.get(key) or default)


def ensure_config_template(path=CONFIG_PATH):
    """Create an empty config.json if none exists. Returns True when one was created."""
    if os.path.exists(path):
        return False
    template = {k: ("your-email@example.com" if k == "CONTACT_EMAIL" else "") for k in CONFIG_KEYS}
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(template, f, indent=2)
        return True
    except OSError as exc:
        log.warning("Could not create %s: %s", path, exc)
        return False


_external_config = load_config()


def _setting(key, default=""):
    return _pick(key, _external_config, os.environ, default)


CONTACT_EMAIL = _setting("CONTACT_EMAIL", "your-email@example.com")
DISCOGS_TOKEN = _setting("DISCOGS_TOKEN")
GENIUS_CLIENT_ID = _setting("GENIUS_CLIENT_ID")
GENIUS_CLIENT_SECRET = _setting("GENIUS_CLIENT_SECRET")
LASTFM_API_KEY = _setting("LASTFM_API_KEY")      # optional: falls back to scraping if unset

MB_MIN_SCORE = 90          # MusicBrainz search score required to trust a release
ALLOW_UNMAPPED = False     # True = let tags outside MASTER_GENRES win a vote

# Bump this after editing GENRE_ALIASES / GENRE_REGEXES / MASTER_GENRES (or fixing how
# artists are looked up): folders and cached artists saved under a different version are
# re-evaluated on the next run. Records saved before versioning existed count as current.
TAXONOMY_VERSION = "1.4"

# Minimum seconds between requests to the same host.
RATE_LIMITS = {
    "www.theaudiodb.com": 0.5,
    "www.last.fm": 0.5,
    "ws.audioscrobbler.com": 0.25,
    "api.discogs.com": 1.0,      # 60 requests/min when authenticated
    "itunes.apple.com": 2.0,     # Apple documents roughly 20/min; raise if you see HTTP 403
    "api.genius.com": 0.5,
    "genius.com": 1.0,
}
DEFAULT_INTERVAL = 0.3

# Polled together first; the remaining services only run if these two don't agree.
FAST_SERVICES = ("Discogs", "Last.fm")

TERMINAL_STATUSES = {"tagged", "matched", "declined"}   # folders we won't revisit
TAG_KEYS = ("artist", "albumartist", "album", "compilation", "genre")
VARIOUS_ARTISTS = {"various artists", "various", "va", "v/a", "v.a."}


def _alt(*terms):
    """Word-bounded alternation, longest terms first so 'folk rock' beats 'folk'."""
    return r"\b(?:" + "|".join(sorted(terms, key=len, reverse=True)) + r")\b"


# 1. Exact-match aliases (compared after prep_tag(): lowercase, no accents, no punctuation)
GENRE_ALIASES = {
    "rnb": "R&B",
    "stage & screen": "Soundtrack",
    "score": "Soundtrack",
    "ost": "Soundtrack",
    # Bare umbrella terms that no compound pattern should claim
    "rock": "Rock",
    "pop": "Pop",
    "alternative": "Alternative Rock",
    "indie": "Alternative Rock",
    "dance": "Electronic",
    "latin": "Latin",
    "world": "World",
}

# 2. Regex consolidations. The match covering the MOST text wins; ties go to the
#    earlier entry. Patterns see prepped text (hyphens/slashes are spaces).
GENRE_REGEXES = [
    (_alt("rap", "hip ?hop", "boom bap", "lo ?fi hip ?hop", "trap", "grime", "drill", "crunk",
          "turntablism", "g ?funk", "cloud rap", "mumble rap", "chopped and screwed",
          "conscious hip ?hop", "abstract hip ?hop", "alternative hip ?hop"), "Hip Hop"),
    (_alt("r&b", "rhythm and blues", "neo ?soul", "quiet storm", "new jack swing", "r&b soul",
          "soul r&b", "alternative r&b", "contemporary r&b"), "R&B"),
    (_alt("funk", "soul", "motown", "disco", "doo ?wop", "p ?funk", "boogie", "memphis soul",
          "classic soul", "northern soul"), "Soul"),
    (_alt("alt(?:ernative)? rock", "indie rock", "grunge", "shoegaze", "post ?rock", "britpop",
          "math rock", "noise rock", "slowcore", "post ?punk", "dream pop", "goth rock",
          "gothic rock"), "Alternative Rock"),
    (_alt("rock ?(?:&|and|n) ?roll", "classic rock", "hard rock", "prog(?:ressive)? rock",
          "psych(?:edelic)? rock", "blues rock", "surf rock", "arena rock", "soft rock",
          "heartland rock", "yacht rock", "southern rock", "rap rock", "funk rock",
          "pop rock"), "Rock"),
    (_alt("techno", "house", "trance", "dubstep", "idm", "drum ?(?:and|n|&) ?bass", "dnb", "jungle",
          "ambient", "synth ?wave", "chillout", "electronic", "electro", "breakbeat", "downtempo",
          "trip ?hop", "uk garage", "chill ?wave", "vaporwave", "hardstyle", "gabber", "ebm",
          "industrial", "darkwave", "edm", "electronica", "happy hardcore",
          "hardcore techno"), "Electronic"),
    # "<anything> metal" so rap/funk/industrial/celtic/christian metal all land here
    (_alt(r"(?:\w+ )?metal", "metalcore", "djent", "grindcore", "deathcore"), "Metal"),
    (_alt("post ?hardcore", "hardcore", "screamo", "punk rock", "pop punk", "skate punk",
          "garage punk", "emo", "crust", "psychobilly", "riot grrrl", "ska punk",
          "anarcho ?punk", "punk"), "Punk"),
    (_alt("reggae", "roots reggae", "lovers rock", "reggae fusion"), "Reggae"),
    (_alt("ska", "2 tone", "two tone", "traditional ska"), "Ska"),
    (_alt("dancehall", "ragga", "bashment"), "Dancehall"),
    (_alt("dub", "dub poetry"), "Dub"),
    (_alt("rocksteady"), "Rocksteady"),
    (_alt("indie pop", "synth ?pop", "electro ?pop", "k ?pop", "j ?pop", "dance pop", "art pop",
          "chamber pop", "hyper ?pop", "bubblegum pop", "city pop", "sophisti ?pop", "power pop",
          "baroque pop", "teen pop"), "Pop"),
    (_alt("bluegrass", "americana", "country", "honky tonk", "alt country", "alternative country",
          "outlaw country", "nashville sound"), "Country"),
    (_alt("jazz", "bebop", "swing", "bossa nova", "hard bop", "free jazz", "jazz fusion",
          "cool jazz", "smooth jazz", "dixieland", "acid jazz", "gypsy jazz"), "Jazz"),
    (_alt("folk", "singer songwriter", "anti ?folk", "neo ?folk", "freak folk", "indie folk",
          "traditional folk", "folk rock"), "Folk"),
    (_alt("delta blues", "chicago blues", "electric blues", "piedmont blues", "jump blues",
          "boogie woogie", "blues"), "Blues"),
    (_alt("reggaeton", "salsa", "bachata", "cumbia", "merengue", "tejano", "latin pop", "norteno",
          "mariachi", "tango", "ranchera", "latin rock", "rumba", "latino"), "Latin"),
    (_alt("classical", "neo ?classical", "baroque", "choral", "orchestral", "symphony", "opera",
          "chamber music", "chamber orchestra", "modern classical", "contemporary classical",
          "minimalist", "romantic era"), "Classical"),
    (_alt("afro ?beats?", "highlife", "kwaito", "bollywood", "celtic", "world ?beat", "world music",
          "worldwide", "fado", "flamenco", "klezmer", "qawwali", "zouk"), "World"),
    (_alt("soundtracks?", "film score", "motion picture", "video game", "anime theme", "musicals?",
          "broadway", "stage ?(?:&|and) ?screen"), "Soundtrack"),
    (_alt("comedy", "stand ?up", "novelty", "parody", "spoken word"), "Comedy"),
    (_alt("gospel", "christian", "worship", "ccm", "praise"), "Gospel"),
    (_alt("holiday", "christmas", "seasonal"), "Holiday"),
    (_alt("childrens", "children", "kids", "nursery"), "Children's"),
    (_alt("new age"), "New Age"),
]

# 3. Valid Master Genres
MASTER_GENRES = {
    "Hip Hop", "R&B", "Soul", "Alternative Rock", "Rock", "Electronic",
    "Metal", "Punk", "Reggae", "Ska", "Dancehall", "Dub", "Rocksteady",
    "Pop", "Country", "Jazz", "Folk", "Blues", "Latin", "Classical",
    "World", "Soundtrack", "Comedy", "Gospel", "Holiday", "Children's", "New Age",
}

# Junk tags (whole-word matching, so "classical" is NOT caught by "classic")
INVALID_TAG_RE = re.compile(
    r"\b(?:seen live|favou?rites?|awesome|vinyl|cd|cassette|mp3|download|owned|albums? i own|"
    r"my library|buy|1001 albums|best of|collection|masterpiece|non music|non-music|"
    r"(?:fe)?male vocalists?|spotify|various(?: artists)?)\b"
)
DECADE_OR_YEAR_RE = re.compile(r"(?:\d{2}|\d{4})s?")

# "Various Artists" style folder names. Word-aware so "Nova - ..." or "Diva - ..." don't match.
COMP_FOLDER_RE = re.compile(
    r"various artists|various\s*[-\u2013]|^(?:va|v\.a\.|v/a)(?![a-z0-9])|"
    r"(?<![a-z0-9])(?:va|v\.a\.)\s*[-\u2013]"
)

FEAT_SPLIT_RE = re.compile(
    r"\s*[(\[]\s*(?:feat\.?|ft\.?|featuring)\b|\s+(?:feat\.?|ft\.?|featuring)\s+|\s*;\s*",
    re.IGNORECASE,
)
# Collaboration separators. "AC/DC" has no spaces around the slash, so it is never split.
COLLAB_SPLIT_RE = re.compile(r"\s+&\s+|,\s+|\s+/\s+")


# ==========================================
# PURE HELPERS (no network; covered by --selftest)
# ==========================================
def strip_accents(text):
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def prep_tag(tag):
    """Lowercase, drop accents/apostrophes, turn - / _ . etc. into spaces ('&' is kept)."""
    t = strip_accents(tag).lower().replace("'", "").replace("\u2019", "")
    t = re.sub(r"[-/_.,;:()\[\]]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


_ALIAS_MAP = {prep_tag(k): v for k, v in GENRE_ALIASES.items()}
_REGEX_MAP = [(re.compile(p), g) for p, g in GENRE_REGEXES]


def normalize_genre(tag):
    """Map a raw tag to a Master Genre. Exact alias first, then the longest regex match."""
    prepped = prep_tag(tag)
    if prepped in _ALIAS_MAP:
        return _ALIAS_MAP[prepped]
    best, best_len = None, 0
    for rx, master in _REGEX_MAP:
        m = rx.search(prepped)
        if m and len(m.group(0)) > best_len:
            best, best_len = master, len(m.group(0))
    return best or tag.title().strip()


def is_valid_genre(tag):
    if not tag:
        return False
    t = tag.lower().strip()
    return not INVALID_TAG_RE.search(t) and not DECADE_OR_YEAR_RE.fullmatch(t)


def is_various(name):
    return bool(name) and name.strip().lower() in VARIOUS_ARTISTS


def artist_candidates(raw_artist):
    """Artist names to try, best first. The full name (minus 'feat.' credits) always comes
    first because real band names contain '&' and ',' (Hall & Oates, Earth, Wind & Fire);
    the first artist of a collaboration is only a fallback."""
    base = FEAT_SPLIT_RE.split(raw_artist)[0].strip() or raw_artist.strip()
    first = COLLAB_SPLIT_RE.split(base)[0].strip()
    return [base] + ([first] if first and first != base else [])


def primary_artist_of(raw_artist):
    return artist_candidates(raw_artist)[0]


def clean_album_title(raw):
    t = re.sub(r'\(.*?\)|\[.*?\]', '', raw).strip()
    t = re.sub(r'[_:\-]', ' ', t)
    t = re.sub(r'\bvol\.?\s*(\d+)\b', r'Volume \1', t, flags=re.IGNORECASE)
    t = re.sub(r'\s+', ' ', t).strip()
    if t.lower().startswith("the ") and len(t) > 4:
        t = t[4:].strip()
    return t or raw


def _sim_norm(text):
    t = strip_accents(text).lower()
    stripped = re.sub(r"\(.*?\)|\[.*?\]", " ", t)
    t = stripped if re.search(r"\w", stripped) else t
    t = re.sub(r"[\W_]+", " ", t).strip()
    return t[4:] if t.startswith("the ") else t


def similar(a, b):
    """0..1 fuzzy match used to confirm a search hit is really the thing we asked for."""
    na, nb = _sim_norm(a or ""), _sim_norm(b or "")
    if not na or not nb:
        return 0.0
    ratio = SequenceMatcher(None, na, nb).ratio()
    if min(len(na), len(nb)) >= 6 and (na in nb or nb in na):
        ratio = max(ratio, 0.9)
    return ratio


def _id3_major(path):
    """ID3v2 major version of the file (3 or 4); 4 when there is no tag yet."""
    with open(path, "rb") as f:
        head = f.read(10)
    return head[3] if head[:3] == b"ID3" and head[3] in (3, 4) else 4


def _genre_frames(tags):
    return [t for t in tags["TCON"].text if t] if "TCON" in tags else None


def write_genre(path, genre):
    """Write TCON, keeping ID3v2.3 files at v2.3 (v2.4 breaks some players) and creating a
    tag for files that have none. Returns the previous raw genre values (a list) or None
    if the file had no genre, which is exactly what restore_genre() needs to undo it."""
    major = _id3_major(path)
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    previous = _genre_frames(tags)
    encoding = 3 if major == 4 else 1          # UTF-8 only exists in v2.4
    tags.setall("TCON", [TCON(encoding=encoding, text=[genre])])
    tags.save(path, v2_version=major)
    return previous or None


def restore_genre(path, old, expected, force=False, dry_run=False):
    """Undo write_genre(). Returns 'restored', 'changed' (the genre is no longer what we
    wrote, so it was edited since; pass force=True to restore anyway) or 'missing'."""
    if not os.path.exists(path):
        return "missing"
    major = _id3_major(path)
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    if not force and _genre_frames(tags) != [expected]:
        return "changed"
    if not dry_run:
        if old:
            tags.setall("TCON", [TCON(encoding=3 if major == 4 else 1, text=list(old))])
        else:
            tags.delall("TCON")
        tags.save(path, v2_version=major)
    return "restored"


def timed_prompt(prompt_text, timeout=5):
    """Cross-platform non-blocking input prompt (empty string on timeout)."""
    sys.stdout.write(prompt_text)
    sys.stdout.flush()

    if sys.platform == "win32":
        import msvcrt
        start_time = time.time()
        ans = ""
        while time.time() - start_time < timeout:
            if msvcrt.kbhit():
                char = msvcrt.getwch()
                if char in ('\r', '\n'):
                    print()
                    return ans.lower().strip()
                if char in ('\x00', '\xe0'):      # arrow/function key: swallow second code
                    msvcrt.getwch()
                elif char == '\x08':              # backspace
                    if ans:
                        ans = ans[:-1]
                        sys.stdout.write('\b \b')
                        sys.stdout.flush()
                else:
                    ans += char
                    sys.stdout.write(char)
                    sys.stdout.flush()
            time.sleep(0.05)
        print()
        return ""
    else:
        import select
        i, _, _ = select.select([sys.stdin], [], [], timeout)
        if i:
            return sys.stdin.readline().strip().lower()
        print()
        return ""


class HostThrottle:
    """Enforces a minimum gap between requests to the same host (thread-safe)."""

    def __init__(self, intervals, default=DEFAULT_INTERVAL):
        self.intervals, self.default = intervals, default
        self.next_ok = {}
        self.lock = threading.Lock()

    def wait(self, host):
        with self.lock:
            now = time.monotonic()
            slot = max(now, self.next_ok.get(host, 0.0))
            self.next_ok[host] = slot + self.intervals.get(host, self.default)
        if slot > now:
            time.sleep(slot - now)


# ==========================================
# GENRE LOOKUP
# ==========================================
class GenreFinder:
    def __init__(self, services=None):
        self.session = requests.Session()
        retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        musicbrainzngs.set_useragent("GenreShark", __version__, CONTACT_EMAIL)   # also rate-limits to 1 req/s

        self.throttle = HostThrottle(RATE_LIMITS)
        self._tls = threading.local()           # last_error is per worker thread
        self._genius_token = None
        self._genius_token_tried = False
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=6)

        # Order matters only for tie-breaking: earlier services win ties.
        all_services = [
            ("Discogs", self.search_discogs),
            ("Last.fm", self.search_lastfm),
            ("iTunes", self.search_itunes),
            ("TheAudioDB", self.search_theaudiodb),
            ("Genius", self.search_genius),
            ("MusicBrainz", self.search_musicbrainz),
        ]
        slug = lambda s: re.sub(r"\W", "", s.lower())
        if services:
            wanted = {slug(s) for s in services}
            known = {slug(n) for n, _ in all_services}
            unknown = wanted - known
            if unknown:
                raise ValueError(f"Unknown service(s): {', '.join(sorted(unknown))}. "
                                 f"Choose from: {', '.join(n for n, _ in all_services)}")
            all_services = [(n, f) for n, f in all_services if slug(n) in wanted]
        self.services = all_services

    def close(self):
        try:
            self.executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:                        # Python < 3.9
            self.executor.shutdown(wait=False)

    @property
    def last_error(self):
        return getattr(self._tls, "last_error", None)

    @last_error.setter
    def last_error(self, value):
        self._tls.last_error = value

    # ---------- plumbing ----------
    def _get_genius_token(self):
        if self._genius_token_tried:
            return self._genius_token
        self._genius_token_tried = True
        if not GENIUS_CLIENT_ID or not GENIUS_CLIENT_SECRET:
            return None
        try:
            resp = self.session.post(
                "https://api.genius.com/oauth/token",
                data={"grant_type": "client_credentials", "client_id": GENIUS_CLIENT_ID,
                      "client_secret": GENIUS_CLIENT_SECRET},
                timeout=10)
            resp.raise_for_status()
            self._genius_token = resp.json().get("access_token")
        except (requests.RequestException, ValueError) as exc:
            log.warning("Genius token request failed: %s", exc)
        return self._genius_token

    def _fetch(self, url, params=None, headers=None, as_json=True):
        self.throttle.wait(urllib.parse.urlsplit(url).netloc)
        try:
            resp = self.session.get(url, params=params, headers=headers, timeout=8)
        except requests.RequestException as exc:
            self.last_error = type(exc).__name__
            log.debug("%s -> %s", url, exc)
            return None
        if resp.status_code == 404:                 # simply "not found"
            return None
        if resp.status_code != 200:
            self.last_error = f"HTTP {resp.status_code}"
            log.debug("%s -> HTTP %s", url, resp.status_code)
            return None
        try:
            return resp.json() if as_json else resp.text
        except ValueError:
            self.last_error = "bad JSON"
            return None

    @staticmethod
    def _valid(tags, limit=5):
        out = []
        for t in tags or []:
            if isinstance(t, str):
                t = t.strip()
                if is_valid_genre(t) and t not in out:
                    out.append(t)
        return out[:limit]

    # ---------- sources (each returns an ordered list of candidate tags) ----------
    def search_theaudiodb(self, artist, album):
        # TADB indexes "Various Artists" as Rock; block it to prevent poisoning.
        if not artist or is_various(artist):
            return []
        data = self._fetch("https://www.theaudiodb.com/api/v1/json/123/search.php", params={"s": artist})
        for a in (data or {}).get("artists") or []:
            if similar(a.get("strArtist", ""), artist) >= 0.85:
                return self._valid([a.get("strGenre"), a.get("strStyle")])
        return []

    def search_lastfm(self, artist, album):
        if not artist:
            return []
        if LASTFM_API_KEY:
            params = {"method": "album.gettoptags" if album else "artist.gettoptags",
                      "artist": artist, "api_key": LASTFM_API_KEY, "format": "json", "autocorrect": 1}
            if album:
                params["album"] = album
            data = self._fetch("https://ws.audioscrobbler.com/2.0/", params=params)
            tags = ((data or {}).get("toptags") or {}).get("tag") or []
            if isinstance(tags, dict):
                tags = [tags]
            return self._valid([t.get("name") for t in tags])

        if BeautifulSoup is None:
            self.last_error = "bs4 missing (install beautifulsoup4 or set LASTFM_API_KEY)"
            return []
        q = lambda s: urllib.parse.quote(s, safe="")
        url = f"https://www.last.fm/music/{q(artist)}/{q(album)}" if album else f"https://www.last.fm/music/{q(artist)}"
        html = self._fetch(url, headers={"User-Agent": USER_AGENT}, as_json=False)
        if not html:
            return []
        section = BeautifulSoup(html, 'html.parser').find('ul', class_='tags-list')
        return self._valid([a.get_text(strip=True) for a in section.find_all('a')]) if section else []

    def search_discogs(self, artist, album):
        if not DISCOGS_TOKEN or not (artist or album):
            return []
        headers = {"User-Agent": USER_AGENT, "Authorization": f"Discogs token={DISCOGS_TOKEN}"}
        params = {"type": "release", "per_page": 5}
        if album:
            params["release_title"] = album
        if artist and not is_various(artist):
            params["artist"] = artist
        results = (self._fetch("https://api.discogs.com/database/search", params=params, headers=headers) or {}).get("results") or []

        def tags_of(r):   # "style" is more specific than "genre", so it goes first
            return self._valid((r.get("style") or []) + (r.get("genre") or []))

        if album:
            for r in results:
                if self._discogs_matches(r.get("title", ""), artist, album):
                    tags = tags_of(r)
                    if tags:                       # a matching release with no tags: try the next
                        return tags
            return []
        # Artist-only: artist search results carry no genres, so pool the artist's releases.
        counts = Counter()
        for r in results:
            if self._discogs_matches(r.get("title", ""), artist, None):
                counts.update(tags_of(r))
        return [t for t, _ in counts.most_common(5)]

    @staticmethod
    def _discogs_matches(title, artist, album):
        artist_part, sep, album_part = title.partition(" - ")
        if not sep:
            artist_part, album_part = "", title
        artist_part = re.sub(r"\s*\(\d+\)$", "", artist_part)       # Discogs "Name (2)" disambiguation
        if artist and not is_various(artist) and artist_part and similar(artist_part, artist) < 0.7:
            return False
        return album is None or similar(album_part, album) >= 0.7

    def search_itunes(self, artist, album):
        query = f"{artist} {album}".strip()
        if not query:
            return []
        params = {"term": query, "media": "music", "entity": "album" if album else "musicArtist", "limit": 5}
        for r in (self._fetch("https://itunes.apple.com/search", params=params) or {}).get("results") or []:
            if album:
                if similar(r.get("collectionName", ""), album) < 0.7:
                    continue
                if artist and not is_various(artist) and similar(r.get("artistName", ""), artist) < 0.7:
                    continue
            elif similar(r.get("artistName", ""), artist) < 0.85:
                continue
            tags = self._valid([r.get("primaryGenreName")])
            if tags:
                return tags
        return []

    def search_musicbrainz(self, artist, album):
        try:
            if album:
                kwargs = {"release": album, "limit": 5}
                if artist:
                    kwargs["artist"] = artist
                for rel in musicbrainzngs.search_releases(**kwargs).get('release-list', []):
                    if int(rel.get('ext:score', 0)) < MB_MIN_SCORE or similar(rel.get('title', ''), album) < 0.7:
                        continue
                    rg_id = (rel.get('release-group') or {}).get('id')
                    if not rg_id:
                        continue
                    rg = musicbrainzngs.get_release_group_by_id(rg_id, includes=["tags"]).get('release-group', {})
                    tags = self._by_count(rg.get('tag-list', []))
                    if tags:
                        return tags
                return []
            if not artist:
                return []
            for art in musicbrainzngs.search_artists(artist=artist, limit=3).get('artist-list', []):
                if int(art.get('ext:score', 0)) >= MB_MIN_SCORE and similar(art.get('name', ''), artist) >= 0.85:
                    tags = self._by_count(art.get('tag-list', []))
                    if tags:
                        return tags
        except (musicbrainzngs.WebServiceError, KeyError, ValueError) as exc:
            self.last_error = type(exc).__name__
            log.debug("MusicBrainz error: %s", exc)
        return []

    def _by_count(self, tag_list):
        ordered = sorted(tag_list, key=lambda t: int(t.get('count', 0)), reverse=True)
        return self._valid([t.get('name') for t in ordered])

    def search_genius(self, artist, album):
        # Artist-only searches find random song lyrics, so require an album.
        if BeautifulSoup is None or not album:
            return []
        token = self._get_genius_token()
        if not token:
            return []
        data = self._fetch("https://api.genius.com/search", params={"q": f"{artist} {album}".strip()},
                           headers={"Authorization": f"Bearer {token}"})
        for hit in ((data or {}).get("response") or {}).get("hits", [])[:5]:
            result = hit.get("result") or {}
            if artist and not is_various(artist):
                if similar((result.get("primary_artist") or {}).get("name", ""), artist) < 0.7:
                    continue
            url = result.get("url")
            if not url:
                continue
            html = self._fetch(url, headers={"User-Agent": USER_AGENT}, as_json=False)
            if not html:
                continue
            soup = BeautifulSoup(html, 'html.parser')
            tags = self._valid([a.get_text(strip=True) for a in soup.find_all('a', href=re.compile(r'/tags/'))])
            if tags:
                return tags
        return []

    # ---------- voting ----------
    def _poll_worker(self, name, func, artist, album):
        """Runs one source in a worker thread; never raises."""
        self.last_error = None
        start = time.time()
        try:
            tags = func(artist, album)
        except Exception as exc:
            tags = []
            self.last_error = type(exc).__name__
            log.debug("%s error: %s", name, exc)
        return name, tags, time.time() - start, self.last_error

    def find_genre(self, artist, album, require_consensus=True):
        """Sources are polled in parallel batches. Each votes for up to 3 Master Genres
        (rank-weighted). A genre backed by two different sources wins immediately. If none
        does, the best single-source genre is used only when require_consensus is False,
        and only after EVERY source has answered, so the result never depends on which
        server happened to reply first. Returns (genre, description) or (None, why)."""
        score, support = {}, {}
        poll_stats, rejected = [], []
        order = {name: i for i, (name, _) in enumerate(self.services)}

        def best_of(candidates):   # highest score; ties go to the earlier-listed service
            return max(candidates, key=lambda g: (score[g], -min(order[n] for n in support[g])))

        batches = [[s for s in self.services if s[0] in FAST_SERVICES],
                   [s for s in self.services if s[0] not in FAST_SERVICES]]
        for batch in batches:
            if not batch:
                continue
            print(f"  [>] Polling {', '.join(n for n, _ in batch)} in parallel...")
            futures = [self.executor.submit(self._poll_worker, n, f, artist, album) for n, f in batch]
            try:
                for future in concurrent.futures.as_completed(futures):
                    name, tags, elapsed, err = future.result()

                    genres = []
                    for tag in tags:
                        g = normalize_genre(tag)
                        if g in MASTER_GENRES or ALLOW_UNMAPPED:
                            if g not in genres:
                                genres.append(g)
                        else:
                            rejected.append(tag)
                    genres = genres[:3]
                    for rank, g in enumerate(genres):
                        score[g] = score.get(g, 0.0) + 1.0 / (rank + 1)
                        support.setdefault(g, set()).add(name)

                    if genres:
                        poll_stats.append(f"{name}[{elapsed:.1f}s]={'/'.join(tags[:3])}->{'/'.join(genres)}")
                    elif err:
                        poll_stats.append(f"{name}[{elapsed:.1f}s]=ERROR({err})")
                    elif tags:
                        poll_stats.append(f"{name}[{elapsed:.1f}s]=unmapped({'/'.join(tags[:2])})")
                    else:
                        poll_stats.append(f"{name}[{elapsed:.1f}s]=None")

                    agreed = [g for g in score if len(support[g]) >= 2]
                    if agreed:
                        best = best_of(agreed)
                        return best, f"Early Consensus ({len(support[best])} services) | Stats: {', '.join(poll_stats)}"
            finally:
                for f in futures:                # stragglers can't be recalled; the rest never start
                    f.cancel()

        stats = f"Stats: {', '.join(poll_stats)}"
        if not score:
            note = f"Rejected Unmapped Tags {rejected} | " if rejected else ""
            return None, f"{note}{stats}"
        best = best_of(list(score))
        if require_consensus:
            return None, f"No Consensus (best '{best}' had 1 source) | {stats}"
        return best, f"Single-source ({', '.join(sorted(support[best]))}) | {stats}"


# ==========================================
# APPLICATION
# ==========================================
REPORT_COLUMNS = ("mode", "folder", "file", "artist", "album", "old_genre", "new_genre", "action", "source")


class MP3TaggerApp:
    def __init__(self, target_dir, args, needs_finder=True):
        self.target_dir = os.path.abspath(target_dir)
        self.args = args
        self.run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.state_file = os.path.join(self.target_dir, "tagger_state.jsonl")
        self.legacy_log = os.path.join(self.target_dir, "tagger_progress.log")
        self.fail_file = os.path.join(self.target_dir, "failed_searches.txt")
        self.cache_file = os.path.join(self.target_dir, "artist_cache.json")
        self.undo_file = os.path.join(self.target_dir, "genre_undo.jsonl")

        # A dry run always produces a report; otherwise only when --report is given.
        self.report_path = args.report or (
            os.path.join(self.target_dir, f"genre_dry_run_{self.run_id}.csv") if args.dry_run else None)
        self._report_file = self._report_writer = None

        self.finder = GenreFinder(services=args.services) if needs_finder else None
        self.processed = set() if args.reprocess else self._load_history()
        self.artist_cache = self._load_cache()
        self.overwrite_policy = "all" if args.overwrite else "none" if args.no_overwrite else None
        self.stats = Counter()

    # ---------- persistence ----------
    def _key(self, folder):
        """Folder identity relative to the library root, so moving the library keeps history."""
        return os.path.normcase(os.path.relpath(folder, self.target_dir))

    def _load_history(self):
        last_status = {}
        if os.path.exists(self.state_file):
            with open(self.state_file, 'r', encoding='utf-8') as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                        if rec.get("schema", TAXONOMY_VERSION) == TAXONOMY_VERSION:
                            last_status[rec["folder"]] = rec["status"]
                    except (ValueError, KeyError, TypeError):
                        continue
        done = {k for k, s in last_status.items() if s in TERMINAL_STATUSES}
        if os.path.exists(self.legacy_log):               # progress from older versions
            with open(self.legacy_log, 'r', encoding='utf-8') as f:
                for line in f:
                    path = line.strip().split(" | ")[0]
                    try:
                        key = self._key(path) if path else None
                    except ValueError:
                        key = None
                    if key and key not in last_status:
                        done.add(key)
        return done

    def _record(self, root, status, **extra):
        self.stats[status] += 1
        if self.args.dry_run:
            return
        rec = {"folder": self._key(root), "status": status, "schema": TAXONOMY_VERSION,
               "ts": datetime.now().isoformat(timespec="seconds"), **extra}
        with open(self.state_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def _mark_failure(self, root, artist, album, stats, elapsed):
        self._record(root, "failed", artist=artist, album=album)
        self._report(root, "", artist, album, "", "", "no genre found", stats.split(" | Stats:")[0] if stats else "")
        if self.args.dry_run:
            return
        with open(self.fail_file, 'a', encoding='utf-8') as f:
            f.write(f"No genre found | Folder: {root} | Searched: {artist} - {album} | {stats} | Total Time: {elapsed:.1f}s\n")

    def _load_cache(self):
        """Cached artist genres, minus entries from another taxonomy version or whose genre
        is no longer a Master Genre."""
        try:
            with open(self.cache_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data.get("artists"), dict) and "version" in data:
                if data["version"] != TAXONOMY_VERSION:
                    return {}
                data = data["artists"]
            # otherwise: older un-versioned flat file, treated as the current version
            return {k: tuple(v) for k, v in data.items() if v and v[0] in MASTER_GENRES}
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def _save_cache(self):
        if self.args.dry_run or not self.artist_cache:
            return
        tmp = self.cache_file + ".tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({"version": TAXONOMY_VERSION, "artists": self.artist_cache}, f,
                      ensure_ascii=False, indent=0)
        os.replace(tmp, self.cache_file)

    # ---------- undo log ----------
    def _log_undo(self, path, old, new):
        rec = {"run": self.run_id, "ts": datetime.now().isoformat(timespec="seconds"),
               "file": os.path.relpath(path, self.target_dir), "old": old, "new": new}
        with open(self.undo_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def _read_undo_log(self):
        """Returns (writes, undone) where undone is a set of (run, file) already reverted."""
        writes, undone = [], set()
        if not os.path.exists(self.undo_file):
            return writes, undone
        with open(self.undo_file, 'r', encoding='utf-8') as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    if rec.get("undone"):
                        undone.add((rec["run"], rec["file"]))
                    else:
                        writes.append({"run": rec["run"], "ts": rec.get("ts", ""), "file": rec["file"],
                                       "old": rec.get("old"), "new": rec["new"]})
                except (ValueError, KeyError, TypeError):
                    continue
        return writes, undone

    def list_runs(self):
        writes, undone = self._read_undo_log()
        if not writes:
            print("No tag changes have been logged yet.")
            return
        runs = {}
        for w in writes:
            r = runs.setdefault(w["run"], {"files": 0, "pending": 0, "ts": w["ts"]})
            r["files"] += 1
            r["pending"] += (w["run"], w["file"]) not in undone
        print(f"{'RUN ID':<18}{'WHEN':<22}{'FILES':>7}{'CAN UNDO':>10}")
        for run_id, r in runs.items():
            print(f"{run_id:<18}{r['ts']:<22}{r['files']:>7}{r['pending']:>10}")

    def undo(self, which="last", force=False):
        """Revert tag changes recorded in the undo log. which = 'last', 'all' or a run id."""
        dry = self.args.dry_run
        writes, undone = self._read_undo_log()
        pending = [w for w in writes if (w["run"], w["file"]) not in undone]
        if not pending:
            print("Nothing to undo.")
            return
        if which == "last":
            run = pending[-1]["run"]
            selected, scope = [w for w in pending if w["run"] == run], f"run {run}"
        elif which == "all":
            selected, scope = pending, "all runs"
        else:
            selected, scope = [w for w in pending if w["run"] == which], f"run {which}"
            if not selected:
                print(f"No undoable changes for run '{which}'. Use --list-runs to see run ids.")
                return

        print(f"\n[UNDO] {'(dry run) ' if dry else ''}{len(selected)} file(s) from {scope}")
        results, touched = Counter(), set()
        for w in reversed(selected):           # newest first, so 'all' lands on the oldest value
            path = os.path.join(self.target_dir, w["file"])
            status = restore_genre(path, w["old"], w["new"], force=force, dry_run=dry)
            results[status] += 1
            name = w["file"]
            if status == "restored":
                touched.add(os.path.dirname(path))
                target = f"'{', '.join(w['old'])}'" if w["old"] else "no genre tag"
                print(f"  {'[DRY RUN] Would restore' if dry else '+ Restored'} {target}: {name}")
                if not dry:
                    with open(self.undo_file, 'a', encoding='utf-8') as f:
                        f.write(json.dumps({"run": w["run"], "file": w["file"], "undone": True,
                                            "ts": datetime.now().isoformat(timespec="seconds")}) + "\n")
            elif status == "changed":
                print(f"  ~ Skipped (genre edited since tagging; use --force to restore anyway): {name}")
            else:
                print(f"  ! File no longer exists: {name}")

        for folder in sorted(touched):         # non-terminal status => rescanned on the next run
            self._record(folder, "undone", run=scope)
        print(f"\n[UNDO] restored: {results['restored']}, skipped (edited since): {results['changed']}, "
              f"missing: {results['missing']}")

    # ---------- change report (CSV) ----------
    def _open_report(self):
        if not self.report_path:
            return
        self._report_file = open(self.report_path, 'w', newline='', encoding='utf-8-sig')
        self._report_writer = csv.writer(self._report_file)
        self._report_writer.writerow(REPORT_COLUMNS)
        print(f" [i] Writing change report to: {self.report_path}")

    def _report(self, root, filename, artist, album, old, new, action, source=""):
        if self._report_writer is None:
            return
        self._report_writer.writerow(["dry-run" if self.args.dry_run else "applied", self._key(root),
                                      filename, artist, album, old, new, action, source])
        self._report_file.flush()

    def _close_report(self):
        if self._report_file:
            self._report_file.close()
            self._report_file = self._report_writer = None

    # ---------- tag reading ----------
    @staticmethod
    def _clean_tag(audio, key):
        try:
            return str(audio.get(key, [''])[0]).strip()
        except Exception:
            return ""

    def _read_tags(self, path):
        try:
            audio = EasyID3(path)
        except ID3NoHeaderError:
            return dict.fromkeys(TAG_KEYS, "")        # readable file, just no tag yet
        except Exception as exc:
            log.debug("Cannot read %s: %s", path, exc)
            return None
        return {k: self._clean_tag(audio, k) for k in TAG_KEYS}

    @staticmethod
    def _folder_identity(root, readable):
        is_comp = bool(COMP_FOLDER_RE.search(os.path.basename(root).lower()))
        artist = album = ""
        for t in readable:
            if t["compilation"] == "1":
                is_comp = True
            if not (artist and album):
                a = t["albumartist"] or t["artist"]
                if a and t["album"]:
                    artist, album = a, t["album"]
        return artist, album, is_comp

    # ---------- main loop ----------
    def run(self):
        self._open_report()
        try:
            for root, dirs, files in os.walk(self.target_dir):
                dirs.sort()
                if self._key(root) in self.processed:
                    continue
                mp3s = sorted(f for f in files if f.lower().endswith('.mp3'))
                if mp3s:
                    self._process_folder(root, mp3s)
        finally:
            if self.finder:
                self.finder.close()
            self._save_cache()
            self._close_report()

    def _process_folder(self, root, mp3s):
        print(f"\n[SCAN] {root}")
        start = time.time()

        tags_by_file = {f: self._read_tags(os.path.join(root, f)) for f in mp3s}
        readable = [t for t in tags_by_file.values() if t is not None]
        if not readable:
            print("  [-] Skipped: no readable MP3 files.")
            self.stats["skipped"] += 1
            return

        if self.args.only_untagged and all(t["genre"] for t in readable):
            print("  [-] Skipped: every file already has a genre (--only-untagged).")
            self.stats["already_tagged"] += 1
            return

        raw_artist, raw_album, is_compilation = self._folder_identity(root, readable)
        if not raw_artist or not raw_album:
            print("  [-] Skipped: No Artist/Album tags found.")
            self._report(root, "", raw_artist, raw_album, "", "", "skipped: no artist/album tags")
            self.stats["skipped"] += 1
            return

        cands = artist_candidates(raw_artist)
        primary_artist = cands[0]
        literal_various = is_various(primary_artist)
        is_comp_album = is_compilation or literal_various
        cache_key = primary_artist.casefold()

        if not is_comp_album and cache_key in self.artist_cache:
            genre, cached_service = self.artist_cache[cache_key]
            service = f"[ARTIST CACHE] {cached_service}"
            print(f"  [*] Using cached genre for artist '{primary_artist}'")
        else:
            genre, service = self._lookup(cands, raw_album, is_comp_album, literal_various)
            if genre and not is_comp_album:
                self.artist_cache[cache_key] = (genre, service.split(" | Stats:")[0])

        elapsed = time.time() - start
        if not genre:
            print("  \033[91m[FAILURE] No genre found on any service.\033[0m")
            self._mark_failure(root, primary_artist, clean_album_title(raw_album), service, elapsed)
            return

        source = service.split(" | Stats:")[0]
        print(f"  \033[92m[SUCCESS]\033[0m Found Genre: {genre} [{source}]")
        self._apply_genre(root, mp3s, tags_by_file, genre, service, elapsed, raw_artist, raw_album)

    def _lookup(self, cands, raw_album, is_comp_album, literal_various):
        """Try the cleaned album title first, then the raw one (and, for collaborations, the
        first-listed artist). Returns (genre, service text)."""
        find = self.finder.find_genre
        artist = cands[0]
        cleaned = clean_album_title(raw_album)
        variants = [cleaned]
        if raw_album.strip() and raw_album.strip() != cleaned:
            variants.append(raw_album.strip())
        failure_note = None

        if is_comp_album:
            for album in variants:
                if album != variants[0]:
                    print("  [*] Retrying with the original album title.")
                # Step 1: album title only (sidesteps "Various Artists" matching problems)
                genre, service = find("", album, require_consensus=False)
                if genre:
                    return genre, service
                failure_note = failure_note or service
                # Step 2: "Various Artists" (required to trigger Last.fm)
                print("  [*] Album-only search failed. Trying 'Various Artists' fallback.")
                genre, fb = find("Various Artists", album, require_consensus=False)
                if genre:
                    return genre, f"[VA FALLBACK] {fb}"
            # Step 3: curator/label fallback if the artist tag isn't explicitly "VA"
            if not literal_various and artist:
                print(f"  [*] Compilation searches failed. Attempting Label fallback for: {artist}")
                genre, fb = find(artist, "", require_consensus=False)
                if genre:
                    return genre, f"[LABEL FALLBACK] {fb}"
            return None, failure_note

        for cand in cands:
            for album in variants:
                if cand != cands[0]:
                    print(f"  [*] Retrying with first-listed artist: {cand}")
                elif album != variants[0]:
                    print("  [*] Retrying with the original album title.")
                genre, service = find(cand, album, require_consensus=True)
                if genre:
                    return genre, service
                failure_note = failure_note or service
        # Artist-only is looser (one source is enough), so it only ever uses the full name:
        # a bare first-listed artist like "Earth" could belong to a different band entirely.
        print(f"  [*] Album consensus failed. Attempting Artist-Only fallback for: {artist}")
        genre, fb = find(artist, "", require_consensus=False)
        if genre:
            return genre, f"[ARTIST FALLBACK] {fb}"
        return None, failure_note

    def _ask_overwrite(self, differing, genre):
        if self.args.only_untagged or self.overwrite_policy == "none":
            return False
        if self.overwrite_policy == "all":
            return True
        distinct = sorted(set(differing))
        shown = ", ".join(f"'{d}'" for d in distinct[:3]) + (", ..." if len(distinct) > 3 else "")
        ans = timed_prompt(
            f"\033[93m      ? Overwrite {shown} with '{genre}'? "
            f"[y]es / [n]o / [a]ll folders / [s]kip all (5s): \033[0m", 5)
        if ans in ("a", "all"):
            self.overwrite_policy = "all"
            return True
        if ans in ("s", "skip"):
            self.overwrite_policy = "none"
            return False
        return ans in ("y", "yes")

    def _apply_genre(self, root, mp3s, tags_by_file, genre, service, elapsed, artist, album):
        source = service.split(" | Stats:")[0]
        plan, differing, errors = [], [], 0
        for f in mp3s:
            tags = tags_by_file[f]
            if tags is None:
                print(f"      ! Unreadable, skipped: {f}")
                errors += 1
                continue
            existing = tags["genre"]
            if existing:
                if self.args.only_untagged:
                    continue
                if normalize_genre(existing) == genre:
                    print(f"      ~ Skipped (Already matches '{existing}'): {f}")
                    self._report(root, f, artist, album, existing, genre, "already matches", source)
                    continue
                differing.append(existing)
            plan.append((f, existing))

        # A dry run never prompts: it lists every overwrite it would offer.
        preview_only = bool(differing) and self.args.dry_run and self.overwrite_policy is None
        overwrite = True if preview_only else (bool(differing) and self._ask_overwrite(differing, genre))

        modified = 0
        for f, existing in plan:
            path = os.path.join(root, f)
            if existing and not overwrite:
                print(f"      ~ Skipped (kept '{existing}'): {f}")
                self._report(root, f, artist, album, existing, genre, "kept existing", source)
                continue
            if self.args.dry_run:
                if existing:
                    note = " (you would be asked first)" if preview_only else ""
                    print(f"      [DRY RUN] Would overwrite '{existing}' with '{genre}'{note}: {f}")
                    action = "would overwrite (asks first)" if preview_only else "would overwrite"
                else:
                    print(f"      [DRY RUN] Would tag with '{genre}': {f}")
                    action = "would tag"
                self._report(root, f, artist, album, existing, genre, action, source)
                modified += 1
                continue
            try:
                previous = write_genre(path, genre)
            except Exception as exc:
                print(f"      ! Error saving {f}: {exc}")
                errors += 1
                continue
            try:
                self._log_undo(path, previous, genre)
            except OSError as exc:
                print(f"      ! Tagged, but could not write the undo log entry: {exc}")
            print(f"      + Overwrote with '{genre}': {f}" if existing else f"      + Tagged: {f}")
            self._report(root, f, artist, album, existing, genre, "overwrote" if existing else "tagged", source)
            modified += 1

        if errors:
            status = "error"                 # not terminal: retried next run
        elif modified:
            status = "tagged"
        elif differing:
            status = "declined"
        else:
            status = "matched"
        self._record(root, status, genre=genre, service=service, seconds=round(elapsed, 1))


# ==========================================
# SELF-TEST (offline)
# ==========================================
def run_selftest():
    failures = []

    def check(label, got, want):
        if got != want:
            failures.append(f"  FAIL {label}: got {got!r}, expected {want!r}")

    for tag, want in {
        "Hip-Hop/Rap": "Hip Hop", "R&B/Soul": "R&B", "Soul/R&B": "R&B", "Classical": "Classical",
        "Classic Rock": "Rock", "Synth-Pop": "Pop", "Electro-Pop": "Pop", "Singer/Songwriter": "Folk",
        "Rap Metal": "Metal", "Funk Metal": "Metal", "Industrial Metal": "Metal", "Pop Rock": "Rock",
        "Folk Rock": "Folk", "Alternative": "Alternative Rock", "Indie": "Alternative Rock",
        "Dance": "Electronic", "Rock": "Rock", "Pop": "Pop", "Post-Punk": "Alternative Rock",
        "Pop Punk": "Punk", "Neo-Soul": "R&B", "Hardcore Techno": "Electronic",
        "Christian & Gospel": "Gospel", "Worldwide": "World", "K-Pop": "Pop", "Electronic": "Electronic",
        "Blues Rock": "Rock", "Latin": "Latin", "Stage & Screen": "Soundtrack", "Rock & Roll": "Rock",
        "Drum and Bass": "Electronic", "Dubstep": "Electronic", "Norte\u00f1o": "Latin",
        "Alternative Hip Hop": "Hip Hop", "Children's Music": "Children's", "Jazz": "Jazz",
        "Death Metal": "Metal", "Latin Pop": "Latin", "Dance Pop": "Pop", "Boogie-Woogie": "Blues",
        # '<X> metal' must stay Metal even when X is itself a genre keyword
        "Christian Metal": "Metal", "Celtic Metal": "Metal", "Electronic Metal": "Metal",
        "Neoclassical Metal": "Metal", "Orchestral Metal": "Metal", "Country Metal": "Metal",
        # Reggae family
        "Reggae": "Reggae", "Roots Reggae": "Reggae", "Dub": "Dub", "Ska": "Ska",
        "Ska Punk": "Punk", "Dancehall": "Dancehall", "Rocksteady": "Rocksteady",
        "Dub Techno": "Electronic",
    }.items():
        check(f"normalize({tag!r})", normalize_genre(tag), want)

    for g in list(GENRE_ALIASES.values()) + [g for _, g in GENRE_REGEXES]:
        if g not in MASTER_GENRES:
            failures.append(f"  FAIL taxonomy: {g!r} is not in MASTER_GENRES")

    for tag, want in {"Classical": True, "classic rock": True, "Acid Jazz": True, "Rock": True,
                      "seen live": False, "Favorites": False, "1999": False, "80s": False,
                      "female vocalists": False, "cd": False, "": False}.items():
        check(f"is_valid_genre({tag!r})", is_valid_genre(tag), want)

    # Real band names keep their '&' / ',' / '/'; the collaboration split is only a fallback.
    for raw, want in {
        "Simon & Garfunkel": ["Simon & Garfunkel", "Simon"],
        "Hall & Oates": ["Hall & Oates", "Hall"],
        "Earth, Wind & Fire": ["Earth, Wind & Fire", "Earth"],
        "Crosby, Stills & Nash": ["Crosby, Stills & Nash", "Crosby"],
        "AC/DC": ["AC/DC"],
        "Florence and the Machine": ["Florence and the Machine"],
        "Drake feat. Rihanna": ["Drake"], "Drake ft. Rihanna": ["Drake"],
        "Artist (feat. Someone)": ["Artist"], "A; B": ["A"],
    }.items():
        check(f"artist_candidates({raw!r})", artist_candidates(raw), want)

    # config precedence: an empty config value must never hide an environment variable
    check("pick: empty config -> env", _pick("K", {"K": ""}, {"K": "env"}), "env")
    check("pick: config beats env", _pick("K", {"K": "cfg"}, {"K": "env"}), "cfg")
    check("pick: default", _pick("K", {}, {}, "d"), "d")

    for folder, want in {"va - best of 2020": True, "va hits": True, "various artists - x": True,
                         "v.a. - hits": True, "2005 va - summer": True, "nova - album": False,
                         "diva - something": False, "eva cassidy - songbird": False,
                         "nirvana - nevermind": False}.items():
        check(f"COMP_FOLDER_RE({folder!r})", bool(COMP_FOLDER_RE.search(folder)), want)

    check("clean_album 1", clean_album_title("Abbey Road (Remastered)"), "Abbey Road")
    check("clean_album 2", clean_album_title("Now Vol. 3"), "Now Volume 3")
    check("clean_album 3", clean_album_title("(Only Parens)"), "(Only Parens)")
    check("similar close", similar("Abbey Road", "Abbey Road (Remastered 2009)") >= 0.9, True)
    check("similar far", similar("Nevermind", "In Utero") < 0.5, True)

    print("\n".join(failures) if failures else "All self-tests passed.")
    return 1 if failures else 0


# ==========================================
# CLI
# ==========================================
def parse_args(argv=None):
    p = argparse.ArgumentParser(description="MP3 auto-consensus genre tagger")
    p.add_argument("path", nargs="?", help="Folder containing your MP3 library (prompted if omitted)")
    p.add_argument("--dry-run", action="store_true",
                   help="Look up genres and write a CSV report of proposed changes; change nothing else")
    p.add_argument("--report", metavar="FILE.csv",
                   help="Write a CSV of every change, proposed or applied (a dry run does this automatically)")
    p.add_argument("--only-untagged", action="store_true",
                   help="Only tag files with no genre; folders that are fully tagged are skipped with no network calls")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--overwrite", action="store_true", help="Overwrite existing genres without asking")
    g.add_argument("--no-overwrite", action="store_true", help="Never overwrite existing genres (no prompts)")
    p.add_argument("--reprocess", action="store_true", help="Ignore saved progress and rescan every folder")
    p.add_argument("--services", type=lambda s: [x for x in s.split(",") if x.strip()],
                   help="Comma-separated subset of: discogs,lastfm,itunes,theaudiodb,genius,musicbrainz")
    p.add_argument("--undo", action="store_true", help="Revert the most recent tagging run")
    p.add_argument("--undo-run", metavar="RUN_ID|all", help="Revert one specific run (see --list-runs), or 'all'")
    p.add_argument("--list-runs", action="store_true", help="List logged runs that can still be undone")
    p.add_argument("--force", action="store_true",
                   help="With --undo: restore even if the genre was edited after tagging")
    p.add_argument("-v", "--verbose", action="store_true", help="Show request errors and debug detail")
    p.add_argument("--selftest", action="store_true", help="Run offline sanity checks and exit")
    return p.parse_args(argv)


def format_duration(seconds):
    mins, secs = divmod(seconds, 60)
    hours, mins = divmod(mins, 60)
    if hours > 0:
        return f"{int(hours)}h {int(mins)}m {secs:.1f}s"
    if mins > 0:
        return f"{int(mins)}m {secs:.1f}s"
    return f"{secs:.1f} seconds"


def main():
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="      (debug) %(message)s")
    if args.selftest:
        sys.exit(run_selftest())

    undo_mode = args.undo or bool(args.undo_run)
    print("=" * 60)
    print(" GENRESHARK - MULTI-SOURCE GENRE CONSENSUS ")
    print("=" * 60)

    if not (undo_mode or args.list_runs):
        if ensure_config_template():
            print(f"\033[92m [+] Created an empty settings template: {CONFIG_PATH}\033[0m")
        missing = [k for k, v in (("DISCOGS_TOKEN", DISCOGS_TOKEN), ("GENIUS_CLIENT_ID", GENIUS_CLIENT_ID),
                                  ("GENIUS_CLIENT_SECRET", GENIUS_CLIENT_SECRET)) if not v]
        if missing:
            print(f"\033[93m [!] Not set in config.json or the environment: {', '.join(missing)}\033[0m")
            print("     Sources that need them are skipped. Add them to config.json for full coverage.\n")

    target = (args.path or input("Enter the full path to your MP3 folder: ")).strip().strip('"').strip("'")
    if not os.path.isdir(target):
        print(f"Error: The directory '{target}' does not exist.")
        sys.exit(1)

    if args.list_runs or undo_mode:
        app = MP3TaggerApp(target, args, needs_finder=False)
        if args.list_runs:
            app.list_runs()
        else:
            app.undo(which=args.undo_run or "last", force=args.force)
        return

    try:
        app = MP3TaggerApp(target, args)
    except ValueError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    print(f"Run ID: {app.run_id}")
    if args.dry_run:
        print("\n[DRY RUN] No tags, state, cache or undo files will be written; only the report below.")
    print("\nStarting scan... (Press Ctrl+C to stop; finished folders are remembered)")

    start = time.time()
    interrupted = False
    try:
        app.run()
    except KeyboardInterrupt:
        interrupted = True
        print("\n\n[!] Interrupted. Progress for completed folders has been saved.")

    print("\n" + "=" * 60)
    print(" SCAN INTERRUPTED" if interrupted else " SCAN COMPLETE")
    if app.stats:
        print(" " + ", ".join(f"{k}: {v}" for k, v in sorted(app.stats.items())))
    if not args.dry_run and (app.stats["tagged"] or app.stats["error"]):
        print(f" Undo this run with: python GenreShark.py \"{target}\" --undo      (run id {app.run_id})")
    print(f" Grand Total Time: {format_duration(time.time() - start)}")
    print("=" * 60)


if __name__ == "__main__":
    main()
