"""
GenreShark - Auto-Consensus Audio Genre Tagger
====================================================
A multi-source genre tagger for local audio libraries. Polls Discogs, Last.fm, iTunes,
TheAudioDB, Genius and MusicBrainz, lets each source vote with its top tags, and
normalizes sub-genres into clean Master Genres. A genre is only written when at least
two sources agree (or, for compilations / artist fallbacks, when a single source
returns a recognised Master Genre).

Supports all major audio formats handled by Mutagen (MP3, FLAC, M4A/MP4, Ogg, Opus,
WMA/ASF, WAV, AIFF, DSF, WavPack, APE, Musepack, etc.).

Requires Python 3.9+.

Dependencies:
    pip install requests musicbrainzngs mutagen
    pip install beautifulsoup4        # optional: only for Last.fm scraping / Genius
"""

import argparse
import concurrent.futures
import csv
import json
import logging
import os
import re
import secrets
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
    import mutagen
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    from mutagen.easyid3 import EasyID3
    from mutagen.easymp4 import EasyMP4, EasyMP4Tags
    from mutagen.id3 import ID3, TCON, ID3NoHeaderError, ID3Tags
    from mutagen.asf import ASF, ASFUnicodeAttribute
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

if "compilation" not in EasyID3.valid_keys:
    EasyID3.RegisterTextKey("compilation", "TCMP")

if "compilation" not in EasyMP4Tags.Get:   # MP4 stores this as the boolean 'cpil' atom
    EasyMP4Tags.RegisterKey("compilation", getter=lambda mp4, key: ["1"] if mp4.get("cpil") else [])

# ==========================================
# CONFIGURATION & API KEYS (config.json, then environment)
# ==========================================
__version__ = "2.3.0"
USER_AGENT = f"GenreShark/{__version__}"
PLACEHOLDER_EMAIL = "your-email@example.com"
PROJECT_URL = "https://github.com/gmg77/GenreShark"   # contact fallback for MusicBrainz

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
CONFIG_KEYS = ("CONTACT_EMAIL", "DISCOGS_TOKEN", "GENIUS_CLIENT_ID",
               "GENIUS_CLIENT_SECRET", "LASTFM_API_KEY")

SUPPORTED_EXTENSIONS = {
    ".mp3", ".flac", ".ogg", ".oga", ".opus", ".spx",
    ".m4a", ".m4b", ".m4p", ".mp4",
    ".wma", ".asf",
    ".ape", ".wv", ".mpc", ".tta",
    ".aiff", ".aif", ".aifc", ".wav", ".dsf", ".dff",
    ".ofr", ".ofs", ".tak",
}
MP4_EXTENSIONS = {".m4a", ".m4b", ".m4p", ".mp4"}


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
    template = {k: (PLACEHOLDER_EMAIL if k == "CONTACT_EMAIL" else "") for k in CONFIG_KEYS}
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


CONTACT_EMAIL = _setting("CONTACT_EMAIL", PLACEHOLDER_EMAIL)
DISCOGS_TOKEN = _setting("DISCOGS_TOKEN")
GENIUS_CLIENT_ID = _setting("GENIUS_CLIENT_ID")
GENIUS_CLIENT_SECRET = _setting("GENIUS_CLIENT_SECRET")
LASTFM_API_KEY = _setting("LASTFM_API_KEY")      # optional: falls back to scraping if unset

MB_MIN_SCORE = 90          # MusicBrainz search score required to trust a release
ALLOW_UNMAPPED = False     # True = let tags outside MASTER_GENRES win a vote

TAXONOMY_VERSION = "1.5"

# Minimum seconds between requests to the same host.
RATE_LIMITS = {
    "www.theaudiodb.com": 0.5,
    "www.last.fm": 0.5,
    "ws.audioscrobbler.com": 0.25,
    "api.discogs.com": 1.0,      # 60 requests/min when authenticated
    "itunes.apple.com": 3.0,     # Apple documents roughly 20/min (~3.0s interval)
    "api.genius.com": 0.5,
    "genius.com": 1.0,
}
DEFAULT_INTERVAL = 0.3

FAST_SERVICES = ("Discogs", "Last.fm")

TERMINAL_STATUSES = {"tagged", "matched", "declined"}   # folders we won't revisit
TAG_KEYS = ("artist", "albumartist", "album", "compilation", "genre")
VARIOUS_ARTISTS = {"various artists", "various", "va", "v/a", "v.a."}


def _alt(*terms):
    """Word-bounded alternation, longest terms first so 'folk rock' beats 'folk'."""
    return r"\b(?:" + "|".join(sorted(terms, key=len, reverse=True)) + r")\b"


# 1. Exact-match aliases
GENRE_ALIASES = {
    "rnb": "R&B",
    "stage & screen": "Soundtrack",
    "score": "Soundtrack",
    "ost": "Soundtrack",
    "música tropical": "Latin",
    "musica tropical": "Latin",
    # Bare umbrella terms
    "rock": "Rock",
    "pop": "Pop",
    "alternative": "Alternative Rock",
    "indie": "Alternative Rock",
    "dance": "Electronic",
    "latin": "Latin",
    "world": "World",
}

# 2. Regex consolidations
GENRE_REGEXES = [
    (_alt("rap", "hip ?hop", "boom bap", "lo ?fi hip ?hop", "trap", "grime", "drill", "crunk",
          "turntablism", "g ?funk", "cloud rap", "mumble rap", "chopped and screwed",
          "conscious hip ?hop", "abstract hip ?hop", "alternative hip ?hop",
          "hardcore hip ?hop", "hardcore rap", "jazz rap", "jazz hip ?hop", "country rap"), "Hip Hop"),
    (_alt("r&b", "rhythm and blues", "neo ?soul", "quiet storm", "new jack swing", "r&b soul",
          "soul r&b", "alternative r&b", "contemporary r&b"), "R&B"),
    (_alt("funk", "soul", "motown", "disco", "doo ?wop", "p ?funk", "boogie", "memphis soul",
          "classic soul", "northern soul", "psychedelic soul"), "Soul"),
    (_alt("alt(?:ernative)? rock", "indie rock", "grunge", "shoegaze", "post ?rock", "britpop",
          "math rock", "noise rock", "slowcore", "post ?punk", "dream pop", "goth rock",
          "gothic rock"), "Alternative Rock"),
    (_alt("rock ?(?:&|and|n) ?roll", "classic rock", "hard rock", "prog(?:ressive)? rock",
          "psych(?:edelic)? rock", "blues rock", "surf rock", "arena rock", "soft rock",
          "heartland rock", "yacht rock", "southern rock", "rap rock", "funk rock",
          "pop rock", "country rock"), "Rock"),
    (_alt("techno", "house", "trance", "dubstep", "idm", "drum ?(?:and|n|&) ?bass", "dnb", "jungle",
          "ambient", "synth ?wave", "chillout", "electronic", "electro", "breakbeat", "downtempo",
          "trip ?hop", "uk garage", "chill ?wave", "vaporwave", "hardstyle", "gabber", "ebm",
          "industrial", "darkwave", "edm", "electronica", "happy hardcore",
          "hardcore techno"), "Electronic"),
    (_alt(r"(?:\w+ )?metal", "metalcore", "djent", "grindcore", "deathcore"), "Metal"),
    (_alt("post ?hardcore", "hardcore", "screamo", "punk rock", "pop punk", "skate punk",
          "garage punk", "emo", "crust", "psychobilly", "riot grrrl", "ska punk",
          "anarcho ?punk", "folk punk", "punk blues", "blues punk", "gypsy punk",
          "synth ?punk", "dance punk", "punk"), "Punk"),
    (_alt("rockabilly", "boppin hillbilly", "hillbilly bop"), "Rockabilly"),
    (_alt("reggae", "roots reggae", "lovers rock", "reggae fusion"), "Reggae"),
    (_alt("ska", "2 tone", "two tone", "traditional ska"), "Ska"),
    (_alt("dancehall", "ragga", "bashment"), "Dancehall"),
    (_alt("dub", "dub poetry"), "Dub"),
    (_alt("rocksteady"), "Rocksteady"),
    (_alt("indie pop", "synth ?pop", "electro ?pop", "k ?pop", "j ?pop", "dance pop", "art pop",
          "chamber pop", "hyper ?pop", "bubblegum pop", "city pop", "sophisti ?pop", "power pop",
          "baroque pop", "teen pop", "country pop"), "Pop"),
    (_alt("bluegrass", "americana", "country", "honky tonk", "alt country", "alternative country",
          "outlaw country", "nashville sound"), "Country"),
    (_alt("jazz", "bebop", "swing", "bossa nova", "hard bop", "free jazz", "jazz fusion",
          "cool jazz", "smooth jazz", "dixieland", "acid jazz", "gypsy jazz"), "Jazz"),
    (_alt("folk", "singer songwriter", "anti ?folk", "neo ?folk", "freak folk", "indie folk",
          "traditional folk", "folk rock"), "Folk"),
    (_alt("delta blues", "chicago blues", "electric blues", "piedmont blues", "jump blues",
          "boogie woogie", "blues"), "Blues"),
    (_alt("reggaeton", "salsa", "bachata", "cumbia", "merengue", "tejano", "latin pop", "norteno",
          "mariachi", "tango", "ranchera", "latin rock", "latin jazz", "rumba", "latino",
          "música tropical", "musica tropical"), "Latin"),
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
    "Metal", "Punk", "Rockabilly", "Reggae", "Ska", "Dancehall", "Dub", "Rocksteady",
    "Pop", "Country", "Jazz", "Folk", "Blues", "Latin", "Classical",
    "World", "Soundtrack", "Comedy", "Gospel", "Holiday", "Children's", "New Age",
}

INVALID_TAG_RE = re.compile(
    r"\b(?:seen live|favou?rites?|awesome|vinyl|cd|cassette|mp3|download|owned|albums? i own|"
    r"my library|buy|1001 albums|best of|collection|masterpiece|non music|non-music|"
    r"(?:fe)?male vocalists?|spotify|various(?: artists)?|literature|release calendar|"
    r"in english|history|biography|essay|enlightenment|science fiction|television|script|"
    r"folk,? world,? (?:&|and) country)\b",
    re.IGNORECASE
)
DECADE_OR_YEAR_RE = re.compile(r"(?:\d{2}|\d{4})s?")

COMP_FOLDER_RE = re.compile(
    r"various artists|various\s*[-\u2013]|^(?:va|v\.a\.|v/a)(?![a-z0-9])|"
    r"(?<![a-z0-9])(?:va|v\.a\.)\s*[-\u2013]"
)

FEAT_SPLIT_RE = re.compile(
    r"\s*[(\[]\s*(?:feat\.?|ft\.?|featuring)\b|\s+(?:feat\.?|ft\.?|featuring)\s+|\s*;\s*",
    re.IGNORECASE,
)
COLLAB_SPLIT_RE = re.compile(r"\s+&\s+|,\s+|\s+/\s+")


# ==========================================
# PURE HELPERS & MULTI-FORMAT TAG I/O
# ==========================================
def strip_accents(text):
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def prep_tag(tag):
    t = strip_accents(tag).lower().replace("'", "").replace("\u2019", "")
    t = re.sub(r"[-/_.,;:()\[\]]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


_ALIAS_MAP = {prep_tag(k): v for k, v in GENRE_ALIASES.items()}
_REGEX_MAP = [(re.compile(p), g) for p, g in GENRE_REGEXES]


def normalize_genre(tag):
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


def is_supported_audio(filename):
    return os.path.splitext(filename)[1].lower() in SUPPORTED_EXTENSIONS


# Control characters (incl. ESC) and bidi overrides: tag text is attacker-controlled, so it must
# never reach the terminal raw.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u200e\u200f\u202a-\u202e\u2066-\u2069]")


def clean_text(text):
    return _CONTROL_CHARS_RE.sub("", text)


_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value):
    """Neutralise spreadsheet formula injection: cells starting with = + - @ get a leading '."""
    if isinstance(value, str) and value.startswith(_CSV_FORMULA_PREFIXES):
        return "'" + value
    return value


def is_inside(base, path):
    """True when `path` resolves to somewhere within `base` (blocks ../ and absolute escapes)."""
    base = os.path.normcase(os.path.realpath(base))
    path = os.path.normcase(os.path.realpath(path))
    try:
        return os.path.commonpath([base, path]) == base
    except ValueError:   # e.g. different drives on Windows
        return False


def parse_cache(data):
    """Validate the raw contents of artist_cache.json.

    Returns (entries, skipped). Each good entry becomes exactly (genre, source); extra trailing
    elements (e.g. added by the HTML editor) are dropped, and malformed entries are skipped
    instead of aborting the whole scan."""
    if not isinstance(data, dict):
        return {}, 0
    if isinstance(data.get("artists"), dict) and "version" in data:
        if data["version"] != TAXONOMY_VERSION:
            return {}, 0
        data = data["artists"]
    entries, skipped = {}, 0
    for key, value in data.items():
        if (isinstance(key, str) and isinstance(value, (list, tuple)) and len(value) >= 2
                and isinstance(value[0], str) and value[0] in MASTER_GENRES
                and isinstance(value[1], str)):
            entries[key] = (value[0], value[1])
        else:
            skipped += 1
    return entries, skipped


def artist_candidates(raw_artist):
    base = FEAT_SPLIT_RE.split(raw_artist)[0].strip() or raw_artist.strip()
    first = COLLAB_SPLIT_RE.split(base)[0].strip()
    return [base] + ([first] if first and first != base else [])


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


def similar(a, b, allow_substring=False):
    na, nb = _sim_norm(a or ""), _sim_norm(b or "")
    if not na or not nb:
        return 0.0
    da, db = re.findall(r"\d+", na), re.findall(r"\d+", nb)
    if (da or db) and da != db:
        return min(SequenceMatcher(None, na, nb).ratio(), 0.6)
    ratio = SequenceMatcher(None, na, nb).ratio()
    if allow_substring and min(len(na), len(nb)) >= 6 and (na in nb or nb in na):
        ratio = max(ratio, 0.9)
    return ratio


def _id3_major(path):
    with open(path, "rb") as f:
        head = f.read(10)
    return head[3] if head[:3] == b"ID3" and head[3] in (3, 4) else 4


def _genre_frames(tags):
    return [str(t) for t in tags["TCON"].text if t] if "TCON" in tags else None


def _open_audio_tags(path):
    """Open an audio file with Mutagen using the best interface for its format."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".mp3":
        return EasyID3(path)   # raises ID3NoHeaderError when untagged; callers handle it
    if ext in MP4_EXTENSIONS:
        return EasyMP4(path)
    audio = mutagen.File(path, easy=True)
    if audio is not None:
        return audio
    return mutagen.File(path)


def _extract_tag_list(audio, key):
    """Return a list of string values for a given canonical key ('artist', 'genre', etc.) across any Mutagen type."""
    if getattr(audio, "tags", None) is None and not isinstance(audio, (EasyID3, EasyMP4)):
        return []

    # 1. ASF / WMA
    if isinstance(audio, ASF):
        asf_map = {
            "artist": "Author",
            "albumartist": "WM/AlbumArtist",
            "album": "WM/AlbumTitle",
            "compilation": "WM/IsCompilation",
            "genre": "WM/Genre",
        }
        k = asf_map.get(key)
        vals = audio.tags.get(k, []) if k and audio.tags else []
        return [str(v.value if hasattr(v, "value") else v).strip() for v in vals if str(v).strip()]

    # 2. Native ID3 containers (AIFF, WAV, DSF, etc. when not wrapped in EasyID3)
    tags_obj = getattr(audio, "tags", None)
    if isinstance(tags_obj, (ID3, ID3Tags)):
        id3_map = {
            "artist": "TPE1",
            "albumartist": "TPE2",
            "album": "TALB",
            "compilation": "TCMP",
            "genre": "TCON",
        }
        frame_id = id3_map.get(key)
        if frame_id and frame_id in tags_obj:
            frame = tags_obj[frame_id]
            return [str(t).strip() for t in getattr(frame, "text", [frame]) if str(t).strip()]
        return []

    # 3. Easy* interfaces, VorbisComments (FLAC, Ogg, Opus), APEv2 (WavPack, Musepack, APE)
    try:
        raw = audio.get(key)
    except Exception:
        raw = None
    if not raw:
        return []
    if isinstance(raw, (str, int, bool)):
        raw = [raw]
    out = []
    for item in raw:
        if isinstance(item, bool):
            out.append("1" if item else "0")
        else:
            s = str(item).strip()
            if s:
                out.append(s)
    return out


def read_audio_tags(path):
    """Read TAG_KEYS from any Mutagen-supported audio file. Returns dict or None if unreadable."""
    try:
        audio = _open_audio_tags(path)
    except ID3NoHeaderError:
        return dict.fromkeys(TAG_KEYS, "")
    except Exception as exc:
        log.debug("Cannot read %s: %s", path, exc)
        return None
    if audio is None:
        return None

    result = {}
    for k in TAG_KEYS:
        vals = _extract_tag_list(audio, k)
        val = clean_text(vals[0]) if vals else ""
        if k == "compilation" and val.lower() in ("true", "1"):
            val = "1"
        result[k] = val
    return result


def write_genre(path, genre):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".mp3":
        major = _id3_major(path)
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        previous = _genre_frames(tags)
        encoding = 3 if major == 4 else 1
        tags.setall("TCON", [TCON(encoding=encoding, text=[genre])])
        tags.save(path, v2_version=major)
        return previous or None

    audio = _open_audio_tags(path)
    if audio is None:
        raise ValueError("Unsupported or unreadable audio format")

    previous = _extract_tag_list(audio, "genre") or None
    if getattr(audio, "tags", None) is None and not isinstance(audio, (EasyID3, EasyMP4)):
        audio.add_tags()

    if isinstance(audio, ASF):
        audio.tags["WM/Genre"] = [ASFUnicodeAttribute(genre)]
    elif isinstance(getattr(audio, "tags", None), (ID3, ID3Tags)):
        audio.tags.delall("TCON")
        audio.tags.add(TCON(encoding=3, text=[genre]))
    else:
        audio["genre"] = [genre]

    audio.save()
    return previous


def read_genre(path):
    """Current genre values exactly as write_genre() would report them as `previous`."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".mp3":
        try:
            return _genre_frames(ID3(path)) or None
        except ID3NoHeaderError:
            return None
    audio = _open_audio_tags(path)
    if audio is None:
        raise ValueError("Unsupported or unreadable audio format")
    return _extract_tag_list(audio, "genre") or None


def restore_genre(path, old, expected, force=False, dry_run=False):
    if not os.path.exists(path):
        return "missing"
    ext = os.path.splitext(path)[1].lower()

    if ext == ".mp3":
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

    try:
        audio = _open_audio_tags(path)
    except Exception:
        return "missing"
    if audio is None:
        return "missing"

    current = _extract_tag_list(audio, "genre") or None
    if not force and current != [expected]:
        return "changed"

    if not dry_run:
        if getattr(audio, "tags", None) is None and not isinstance(audio, (EasyID3, EasyMP4)):
            audio.add_tags()

        if isinstance(audio, ASF):
            if old:
                audio.tags["WM/Genre"] = [ASFUnicodeAttribute(g) for g in old]
            elif "WM/Genre" in audio.tags:
                del audio.tags["WM/Genre"]
        elif isinstance(getattr(audio, "tags", None), (ID3, ID3Tags)):
            audio.tags.delall("TCON")
            if old:
                audio.tags.add(TCON(encoding=3, text=list(old)))
        else:
            if old:
                audio["genre"] = list(old)
            elif "genre" in audio:
                del audio["genre"]
        audio.save()
    return "restored"


def timed_prompt(prompt_text, timeout=5):
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
                if char in ('\x00', '\xe0'):
                    msvcrt.getwch()
                elif char == '\x08':
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
        retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
                      respect_retry_after_header=True)
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        contact = CONTACT_EMAIL if CONTACT_EMAIL and CONTACT_EMAIL != PLACEHOLDER_EMAIL else PROJECT_URL
        musicbrainzngs.set_useragent("GenreShark", __version__, contact)

        self.throttle = HostThrottle(RATE_LIMITS)
        self._tls = threading.local()
        self._genius_token = None
        self._genius_token_tried = False
        self.failed_queries = set()   # in-memory negative cache for (artist, album, require_consensus, min_sim)
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=6)

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
        except TypeError:
            self.executor.shutdown(wait=False)

    @property
    def last_error(self):
        return getattr(self._tls, "last_error", None)

    @last_error.setter
    def last_error(self, value):
        self._tls.last_error = value

    def _artist_threshold(self, default=0.7):
        override = getattr(self._tls, "min_artist_sim", None)
        return max(default, override) if override is not None else default

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
        if resp.status_code == 404:
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

    def search_theaudiodb(self, artist, album):
        if not artist or is_various(artist):
            return []
        min_sim = self._artist_threshold(0.85)
        data = self._fetch("https://www.theaudiodb.com/api/v1/json/123/search.php", params={"s": artist})
        for a in (data or {}).get("artists") or []:
            if similar(a.get("strArtist", ""), artist) >= min_sim:
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
            toptags = (data or {}).get("toptags") or {}
            # autocorrect=1 can silently swap in a different artist/album, so verify who answered.
            attr = toptags.get("@attr") or {}
            if (artist and not is_various(artist) and attr.get("artist")
                    and similar(attr["artist"], artist) < self._artist_threshold(0.85)):
                log.debug("Last.fm autocorrected artist %r -> %r; ignoring", artist, attr["artist"])
                return []
            if album and attr.get("album") and similar(attr["album"], album, allow_substring=True) < 0.7:
                log.debug("Last.fm autocorrected album %r -> %r; ignoring", album, attr["album"])
                return []
            tags = toptags.get("tag") or []
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
        min_sim = self._artist_threshold(0.7)
        headers = {"User-Agent": USER_AGENT, "Authorization": f"Discogs token={DISCOGS_TOKEN}"}
        params = {"type": "release", "per_page": 5}
        if album:
            params["release_title"] = album
        if artist and not is_various(artist):
            params["artist"] = artist
        results = (self._fetch("https://api.discogs.com/database/search", params=params, headers=headers) or {}).get("results") or []

        def tags_of(r):
            return self._valid((r.get("style") or []) + (r.get("genre") or []))

        if album:
            for r in results:
                if self._discogs_matches(r.get("title", ""), artist, album, min_sim=min_sim):
                    tags = tags_of(r)
                    if tags:
                        return tags
            return []
        counts = Counter()
        for r in results:
            if self._discogs_matches(r.get("title", ""), artist, None, min_sim=min_sim):
                counts.update(tags_of(r))
        return [t for t, _ in counts.most_common(5)]

    @staticmethod
    def _discogs_matches(title, artist, album, min_sim=0.7):
        artist_part, sep, album_part = title.partition(" - ")
        if not sep:
            artist_part, album_part = "", title
        artist_part = re.sub(r"\s*\(\d+\)$", "", artist_part)
        if artist and not is_various(artist) and artist_part and similar(artist_part, artist) < min_sim:
            return False
        return album is None or similar(album_part, album, allow_substring=True) >= 0.7

    def search_itunes(self, artist, album):
        query = f"{artist} {album}".strip()
        if not query:
            return []
        min_sim = self._artist_threshold(0.7 if album else 0.85)
        params = {"term": query, "media": "music", "entity": "album" if album else "musicArtist", "limit": 5}
        for r in (self._fetch("https://itunes.apple.com/search", params=params) or {}).get("results") or []:
            if album:
                if similar(r.get("collectionName", ""), album, allow_substring=True) < 0.7:
                    continue
                if artist and not is_various(artist) and similar(r.get("artistName", ""), artist) < min_sim:
                    continue
            elif similar(r.get("artistName", ""), artist) < min_sim:
                continue
            tags = self._valid([r.get("primaryGenreName")])
            if tags:
                return tags
        return []

    def search_musicbrainz(self, artist, album):
        try:
            if album:
                min_sim = self._artist_threshold(0.7)
                kwargs = {"release": album, "limit": 5}
                if artist:
                    kwargs["artist"] = artist
                seen_rg = set()
                for rel in musicbrainzngs.search_releases(**kwargs).get('release-list', []):
                    if int(rel.get('ext:score', 0)) < MB_MIN_SCORE or similar(rel.get('title', ''), album, allow_substring=True) < 0.7:
                        continue
                    if artist and not is_various(artist):
                        credit = rel.get('artist-credit-phrase', '')
                        if credit and similar(credit, artist) < min_sim:
                            continue
                    rg_id = (rel.get('release-group') or {}).get('id')
                    if not rg_id or rg_id in seen_rg:
                        continue
                    seen_rg.add(rg_id)
                    rg = musicbrainzngs.get_release_group_by_id(rg_id, includes=["tags"]).get('release-group', {})
                    tags = self._by_count(rg.get('tag-list', []))
                    if tags:
                        return tags
                return []
            if not artist:
                return []
            min_sim = self._artist_threshold(0.85)
            for art in musicbrainzngs.search_artists(artist=artist, limit=3).get('artist-list', []):
                if int(art.get('ext:score', 0)) >= MB_MIN_SCORE and similar(art.get('name', ''), artist) >= min_sim:
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
        if BeautifulSoup is None or not album:
            return []
        token = self._get_genius_token()
        if not token:
            return []
        min_sim = self._artist_threshold(0.7)
        data = self._fetch("https://api.genius.com/search", params={"q": f"{artist} {album}".strip()},
                           headers={"Authorization": f"Bearer {token}"})
        for hit in ((data or {}).get("response") or {}).get("hits", [])[:2]:
            result = hit.get("result") or {}
            if artist and not is_various(artist):
                if similar((result.get("primary_artist") or {}).get("name", ""), artist) < min_sim:
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

    def _poll_worker(self, name, func, artist, album, min_artist_sim=None):
        self.last_error = None
        self._tls.min_artist_sim = min_artist_sim
        start = time.time()
        try:
            tags = func(artist, album)
        except Exception as exc:
            tags = []
            self.last_error = type(exc).__name__
            log.debug("%s error: %s", name, exc)
        return name, tags, time.time() - start, self.last_error

    def find_genre(self, artist, album, require_consensus=True, min_artist_sim=None):
        neg_key = (artist.casefold(), album.casefold(), require_consensus, min_artist_sim)
        if neg_key in self.failed_queries:
            return None, "Cached negative lookup | Stats: skipped (previously failed in this run)"

        score, support = {}, {}
        poll_stats, rejected, errored = [], [], []
        order = {name: i for i, (name, _) in enumerate(self.services)}

        def best_of(candidates):
            return max(
                candidates,
                key=lambda g: (score[g], len(support[g]), -min(order[n] for n in support[g]), g),
            )

        def record_result(name, tags, elapsed, err):
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
                errored.append(name)
                poll_stats.append(f"{name}[{elapsed:.1f}s]=ERROR({err})")
            elif tags:
                poll_stats.append(f"{name}[{elapsed:.1f}s]=unmapped({'/'.join(tags[:2])})")
            else:
                poll_stats.append(f"{name}[{elapsed:.1f}s]=None")

        batches = [[s for s in self.services if s[0] in FAST_SERVICES],
                   [s for s in self.services if s[0] not in FAST_SERVICES]]
        for batch in batches:
            if not batch:
                continue
            print(f"  [>] Polling {', '.join(n for n, _ in batch)} in parallel...")
            futures = [self.executor.submit(self._poll_worker, n, f, artist, album, min_artist_sim) for n, f in batch]
            processed_futures = set()
            try:
                for future in concurrent.futures.as_completed(futures):
                    if future not in processed_futures:
                        processed_futures.add(future)
                        record_result(*future.result())

                    # Drain any other futures that have already finished so tie-breaking is deterministic
                    for other in futures:
                        if other not in processed_futures and other.done():
                            processed_futures.add(other)
                            record_result(*other.result())

                    agreed = [g for g in score if len(support[g]) >= 2]
                    if agreed:
                        best = best_of(agreed)
                        return best, f"Early Consensus ({len(support[best])} services) | Stats: {', '.join(poll_stats)}"
            finally:
                for f in futures:
                    f.cancel()

        stats = f"Stats: {', '.join(poll_stats)}"
        if not score:
            if not errored:   # never cache a failure that may just be a network blip
                self.failed_queries.add(neg_key)
            note = f"Rejected Unmapped Tags {rejected} | " if rejected else ""
            return None, f"{note}{stats}"
        best = best_of(list(score))
        if require_consensus:
            if not errored:
                self.failed_queries.add(neg_key)
            return None, f"No Consensus (best '{best}' had 1 source) | {stats}"
        return best, f"Single-source ({', '.join(sorted(support[best]))}) | {stats}"


# ==========================================
# APPLICATION
# ==========================================
REPORT_COLUMNS = ("mode", "folder", "file", "artist", "album", "old_genre", "new_genre", "action", "source")


def replace_with_retry(src, dst, attempts=8, delay=0.25):
    """os.replace() that survives Windows sharing violations (WinError 32/5).

    Antivirus, OneDrive/Dropbox sync, the search indexer or an open editor can briefly
    lock a file we just wrote or are about to replace. Retry with a growing pause."""
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return True
        except PermissionError:
            if attempt == attempts - 1:
                return False
            time.sleep(delay * (attempt + 1))
    return False


def write_json_atomic(path, payload):
    """Write JSON via a temp file + rename. Never raises: returns the path that actually holds
    the data (``path`` normally, a side file if ``path`` is locked), or None on total failure."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=0)
    except OSError as exc:
        print(f"\n [!] Could not write cache file: {exc}")
        return None
    if replace_with_retry(tmp, path):
        return path
    # Destination still locked: keep the work under another name instead of crashing.
    side = f"{os.path.splitext(path)[0]}.{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    if replace_with_retry(tmp, side, attempts=2):
        print(f"\n [!] {os.path.basename(path)} is locked by another program (antivirus, sync client, "
              f"editor or a second copy of this script?).\n     Your results were saved to {side}\n"
              f"     Close whatever has the file open, then rename that file to {os.path.basename(path)}.")
        return side
    print(f"\n [!] Could not replace {path}; your results are in {tmp}.\n"
          f"     Close whatever has the file open, then rename {os.path.basename(tmp)} to {os.path.basename(path)}.")
    return tmp


class MP3TaggerApp:
    def __init__(self, target_dir, args, needs_finder=True):
        self.target_dir = os.path.abspath(target_dir)
        self.args = args
        self.run_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
        self.state_file = os.path.join(self.target_dir, "tagger_state.jsonl")
        self.legacy_log = os.path.join(self.target_dir, "tagger_progress.log")
        self.fail_file = os.path.join(self.target_dir, "failed_searches.txt")
        self.cache_file = os.path.join(self.target_dir, "artist_cache.json")
        self.undo_file = os.path.join(self.target_dir, "genre_undo.jsonl")

        self.is_dry_run = args.dry_run or args.dry_run_cached
        self.allow_cache_write = not args.dry_run

        self.report_path = args.report or (
            os.path.join(self.target_dir, f"genre_dry_run_{self.run_id}.csv") if (args.dry_run or args.dry_run_cached) else None)
        self._report_file = self._report_writer = None

        self.finder = GenreFinder(services=args.services) if needs_finder else None
        self.processed = set() if args.reprocess else self._load_history()
        self.artist_cache = self._load_cache()
        self.overwrite_policy = "all" if args.overwrite else "none" if args.no_overwrite else None
        self.stats = Counter()

    def _key(self, folder):
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
        if os.path.exists(self.legacy_log):
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
        if self.is_dry_run:
            return
        rec = {"folder": self._key(root), "status": status, "schema": TAXONOMY_VERSION,
               "ts": datetime.now().isoformat(timespec="seconds"), **extra}
        with open(self.state_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def _mark_failure(self, root, artist, album, stats, elapsed, existing_genres=None):
        self._record(root, "failed", artist=artist, album=album)
        old_genre_str = ", ".join(sorted(set(existing_genres))) if existing_genres else ""
        self._report(root, "", artist, album, old_genre_str, "", "no genre found", stats.split(" | Stats:")[0] if stats else "")
        if self.is_dry_run:
            return
        with open(self.fail_file, 'a', encoding='utf-8') as f:
            f.write(f"No genre found | Folder: {root} | Searched: {artist} - {album} | {stats} | Total Time: {elapsed:.1f}s\n")

    def _load_cache(self):
        try:
            with open(self.cache_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            print(f" [!] Could not read {os.path.basename(self.cache_file)} ({exc}); starting with an empty cache.")
            return {}
        entries, skipped = parse_cache(data)
        if skipped:
            print(f" [!] Ignored {skipped} malformed or unrecognised entr{'y' if skipped == 1 else 'ies'} "
                  f"in {os.path.basename(self.cache_file)}.")
        return entries

    def _save_cache(self):
        if not self.allow_cache_write or not self.artist_cache:
            return
        write_json_atomic(self.cache_file, {"version": TAXONOMY_VERSION, "artists": self.artist_cache})

    def _append_undo(self, rec):
        """Append one record and force it to disk, so it survives a crash right after."""
        with open(self.undo_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def _log_undo(self, path, old, new):
        """Write-ahead: call this BEFORE modifying the file."""
        self._append_undo({"run": self.run_id, "ts": datetime.now().isoformat(timespec="seconds"),
                           "file": os.path.relpath(path, self.target_dir), "old": old, "new": new})

    def _log_undo_rollback(self, path):
        """The write that _log_undo() announced failed: void that entry so it isn't offered for undo."""
        try:
            self._append_undo({"run": self.run_id, "ts": datetime.now().isoformat(timespec="seconds"),
                               "file": os.path.relpath(path, self.target_dir), "undone": True,
                               "note": "write failed"})
        except OSError as exc:
            log.warning("Could not void undo entry for %s: %s", path, exc)

    def _read_undo_log(self):
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
        print(f"{'RUN ID':<22}{'WHEN':<22}{'FILES':>7}{'CAN UNDO':>10}")
        for run_id, r in runs.items():
            print(f"{run_id:<22}{r['ts']:<22}{r['files']:>7}{r['pending']:>10}")

    def undo(self, which="last", force=False):
        dry = self.is_dry_run
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
        for w in reversed(selected):
            path = os.path.join(self.target_dir, w["file"])
            name = clean_text(str(w["file"]))
            if not is_inside(self.target_dir, path):
                results["unsafe"] += 1
                print(f"  ! Refused (path escapes the library folder): {name}")
                continue
            status = restore_genre(path, w["old"], w["new"], force=force, dry_run=dry)
            results[status] += 1
            if status == "restored":
                touched.add(os.path.dirname(path))
                target = f"'{clean_text(', '.join(str(g) for g in w['old']))}'" if w["old"] else "no genre tag"
                print(f"  {'[DRY RUN] Would restore' if dry else '+ Restored'} {target}: {name}")
                if not dry:
                    with open(self.undo_file, 'a', encoding='utf-8') as f:
                        f.write(json.dumps({"run": w["run"], "file": w["file"], "undone": True,
                                            "ts": datetime.now().isoformat(timespec="seconds")}) + "\n")
            elif status == "changed":
                print(f"  ~ Skipped (genre edited since tagging; use --force to restore anyway): {name}")
            else:
                print(f"  ! File no longer exists: {name}")

        for folder in sorted(touched):
            self._record(folder, "undone", run=scope)
        print(f"\n[UNDO] restored: {results['restored']}, skipped (edited since): {results['changed']}, "
              f"missing: {results['missing']}, refused (unsafe path): {results['unsafe']}")

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
        row = ["dry-run" if self.is_dry_run else "applied", self._key(root),
               filename, artist, album, old, new, action, source]
        self._report_writer.writerow([csv_safe(c) for c in row])
        self._report_file.flush()

    def _close_report(self):
        if self._report_file:
            self._report_file.close()
            self._report_file = self._report_writer = None

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

    def run(self):
        self._open_report()
        try:
            for root, dirs, files in os.walk(self.target_dir):
                dirs.sort()
                if self._key(root) in self.processed:
                    continue
                audio_files = sorted(f for f in files if is_supported_audio(f))
                if audio_files:
                    self._process_folder(root, audio_files)
        finally:
            # Independent steps: a failure in one must not skip the others.
            try:
                if self.finder:
                    self.finder.close()
            finally:
                try:
                    self._save_cache()
                finally:
                    self._close_report()

    def _process_folder(self, root, audio_files):
        print(f"\n[SCAN] {root}")
        start = time.time()

        tags_by_file = {f: read_audio_tags(os.path.join(root, f)) for f in audio_files}
        readable = [t for t in tags_by_file.values() if t is not None]
        if not readable:
            print("  [-] Skipped: no readable audio files.")
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
            existing_genres = [t["genre"] for t in readable if t["genre"]]
            self._mark_failure(root, primary_artist, clean_album_title(raw_album), service, elapsed, existing_genres)
            return

        source = service.split(" | Stats:")[0]
        print(f"  \033[92m[SUCCESS]\033[0m Found Genre: {genre} [{source}]")
        self._apply_genre(root, audio_files, tags_by_file, genre, service, elapsed, raw_artist, raw_album)

    def _lookup(self, cands, raw_album, is_comp_album, literal_various):
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
                genre, service = find("", album, require_consensus=False)
                if genre:
                    return genre, service
                failure_note = failure_note or service
                print("  [*] Album-only search failed. Trying 'Various Artists' fallback.")
                genre, fb = find("Various Artists", album, require_consensus=False)
                if genre:
                    return genre, f"[VA FALLBACK] {fb}"
            if not literal_various and artist:
                print(f"  [*] Compilation searches failed. Attempting Label fallback for: {artist}")
                genre, fb = find(artist, "", require_consensus=False)
                if genre:
                    return genre, f"[LABEL FALLBACK] {fb}"
            return None, failure_note

        for cand in cands:
            is_split_cand = cand != cands[0]
            min_sim = 0.95 if is_split_cand else None
            for album in variants:
                if is_split_cand and album == variants[0]:
                    print(f"  [*] Retrying with first-listed artist (strict match >=0.95): {cand}")
                elif album != variants[0]:
                    print("  [*] Retrying with the original album title.")
                genre, service = find(cand, album, require_consensus=True, min_artist_sim=min_sim)
                if genre:
                    return genre, service
                failure_note = failure_note or service
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

    def _apply_genre(self, root, audio_files, tags_by_file, genre, service, elapsed, artist, album):
        source = service.split(" | Stats:")[0]
        plan, differing, errors = [], [], 0
        for f in audio_files:
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

        preview_only = bool(differing) and self.is_dry_run and self.overwrite_policy is None
        overwrite = True if preview_only else (bool(differing) and self._ask_overwrite(differing, genre))

        modified = 0
        for f, existing in plan:
            path = os.path.join(root, f)
            if existing and not overwrite:
                print(f"      ~ Skipped (kept '{existing}'): {f}")
                self._report(root, f, artist, album, existing, genre, "kept existing", source)
                continue
            if self.is_dry_run:
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
            try:   # write-ahead: no undo record, no change
                self._log_undo(path, read_genre(path), genre)
            except Exception as exc:
                print(f"      ! Skipped (could not record undo entry first): {f}: {exc}")
                errors += 1
                continue
            try:
                write_genre(path, genre)
            except Exception as exc:
                print(f"      ! Error saving {f}: {exc}")
                errors += 1
                self._log_undo_rollback(path)
                continue
            print(f"      + Overwrote with '{genre}': {f}" if existing else f"      + Tagged: {f}")
            self._report(root, f, artist, album, existing, genre, "overwrote" if existing else "tagged", source)
            modified += 1

        if errors:
            status = "error"
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
        "Blues Rock": "Rock", "Latin": "Latin", "Música Tropical": "Latin", "Stage & Screen": "Soundtrack",
        "Rockabilly": "Rockabilly", "Boppin' Hillbilly": "Rockabilly",
        "Drum and Bass": "Electronic", "Dubstep": "Electronic", "Norteño": "Latin",
        "Hardcore Hip Hop": "Hip Hop", "Jazz Rap": "Hip Hop", "Folk Punk": "Punk",
    }.items():
        check(f"is_valid_genre({tag!r})", is_valid_genre(tag), True)
        check(f"normalize({tag!r})", normalize_genre(tag), want)

    for invalid_tag in ("seen live", "Favorites", "1990s", "80s", "albums i own"):
        check(f"is_valid_genre({invalid_tag!r})", is_valid_genre(invalid_tag), False)

    # Artist vs Album substring & digit checks
    check("similar('Prince', 'Prince Royce') < 0.85", similar("Prince", "Prince Royce") < 0.85, True)
    check("similar('Abbey Road', 'Abbey Road Deluxe', allow_substring=True) >= 0.9",
          similar("Abbey Road", "Abbey Road Deluxe", allow_substring=True) >= 0.9, True)
    check("similar('Now Music 45', 'Now Music 54', allow_substring=True) < 0.7",
          similar("Now Music 45", "Now Music 54", allow_substring=True) < 0.7, True)

    for g in list(GENRE_ALIASES.values()) + [g for _, g in GENRE_REGEXES]:
        if g not in MASTER_GENRES:
            failures.append(f"  FAIL taxonomy: {g!r} is not in MASTER_GENRES")

    # Cache loading: entries with extra elements (as round-tripped by the HTML editor) must not
    # crash, and malformed entries must be skipped rather than abort the scan.
    entries, skipped = parse_cache({"version": TAXONOMY_VERSION, "artists": {
        "a": ["Rock", "Discogs, iTunes", "extra", 3],
        "b": ["Rock"], "c": "Rock", "d": ["NotAGenre", "x"], "e": ["Jazz", "MusicBrainz"],
        "f": [None, "x"], "g": None}})
    check("parse_cache entries", entries, {"a": ("Rock", "Discogs, iTunes"), "e": ("Jazz", "MusicBrainz")})
    check("parse_cache skipped", skipped, 5)
    check("parse_cache wrong version", parse_cache({"version": "0.0", "artists": {"a": ["Rock", "x"]}}), ({}, 0))
    check("parse_cache legacy flat", parse_cache({"a": ["Rock", "x"]}), ({"a": ("Rock", "x")}, 0))
    check("parse_cache non-dict", parse_cache([1, 2]), ({}, 0))

    check("csv_safe formula", csv_safe("=1+1"), "'=1+1")
    check("csv_safe '+/-'", csv_safe("+/-"), "'+/-")
    check("csv_safe plain", csv_safe("Beatles"), "Beatles")
    check("csv_safe non-str", csv_safe(5), 5)
    check("clean_text", clean_text("a\x1b[31mb\u202ec"), "a[31mbc")
    check("is_inside ok", is_inside("/lib", "/lib/a/b.mp3"), True)
    check("is_inside ../", is_inside("/lib", "/lib/../etc/passwd"), False)
    check("is_inside absolute", is_inside("/lib", os.path.join("/lib", "/etc/passwd")), False)

    print("\n".join(failures) if failures else "All self-tests passed.")
    return 1 if failures else 0


# ==========================================
# CLI
# ==========================================
def parse_args(argv=None):
    p = argparse.ArgumentParser(description="GenreShark multi-format auto-consensus genre tagger")
    p.add_argument("path", nargs="?", help="Folder containing your music library (prompted if omitted)")
    p.add_argument("--dry-run", action="store_true",
                   help="Look up genres and write a CSV report of proposed changes; change nothing else")
    p.add_argument("--dry-run-cached", action="store_true",
                   help="Like --dry-run, but allows saving resolved artists to artist_cache.json")
    p.add_argument("--report", metavar="FILE.csv",
                   help="Write a CSV of every change, proposed or applied")
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
        if not (args.dry_run or args.dry_run_cached) and ensure_config_template():
            print(f"\033[92m [+] Created an empty settings template: {CONFIG_PATH}\033[0m")
        missing = [k for k, v in (("DISCOGS_TOKEN", DISCOGS_TOKEN), ("GENIUS_CLIENT_ID", GENIUS_CLIENT_ID),
                                  ("GENIUS_CLIENT_SECRET", GENIUS_CLIENT_SECRET)) if not v]
        if missing:
            print(f"\033[93m [!] Not set in config.json or the environment: {', '.join(missing)}\033[0m")
            print("     Sources that need them are skipped. Add them to config.json for full coverage.\n")
        if CONTACT_EMAIL in ("", PLACEHOLDER_EMAIL):
            print("\033[93m [!] CONTACT_EMAIL is not set; MusicBrainz will be sent the project URL instead.\033[0m")
            print("     Set your real email in config.json to identify yourself properly.\n")

    target = (args.path or input("Enter the full path to your music folder: ")).strip().strip('"').strip("'")
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
    if args.dry_run or args.dry_run_cached:
        print("\n[DRY RUN] No tags, state or undo files will be written; only the CSV report.")
        if args.dry_run_cached:
            print("          Artist cache updates ARE enabled (--dry-run-cached).")
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
    if not (args.dry_run or args.dry_run_cached) and (app.stats["tagged"] or app.stats["error"]):
        print(f" Undo this run with: python GenreShark.py \"{target}\" --undo      (run id {app.run_id})")
    print(f" Grand Total Time: {format_duration(time.time() - start)}")
    print("=" * 60)


if __name__ == "__main__":
    main()