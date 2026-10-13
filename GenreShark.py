"""
MP3 Auto-Consensus Genre Tagger
====================================================
A high-speed, multi-source genre tagger for local MP3 libraries.
Polls TheAudioDB, Last.fm, Discogs, iTunes, MusicBrainz, and Genius
to find a strict consensus, and normalizes over 150 sub-genres into
clean, standardized Master Genres using a pre-compiled Regex engine.

Dependencies:
    pip install requests bs4 mutagen musicbrainzngs

Usage:
    Run the script and provide the absolute path to your MP3 directory.
    To enable Discogs and Genius, add your API credentials to the 
    CONFIGURATION block below.
"""

import os
import re
import sys
import time
import urllib.parse
import unicodedata
from collections import Counter

# --- DEPENDENCY CHECK ---
try:
    import requests
    import musicbrainzngs
    from bs4 import BeautifulSoup
    from mutagen.easyid3 import EasyID3
    from mutagen.id3 import ID3NoHeaderError
except ImportError as e:
    module_name = str(e).split("'")[1] if "'" in str(e) else str(e)
    print("=" * 60)
    print(f" [!] MISSING REQUIRED MODULE: {module_name}")
    print(f"     Please run: pip install {module_name}")
    sys.exit(1)

# Enable ANSI escape codes for Windows terminals
if sys.platform == 'win32':
    os.system('')

# ==========================================
# CONFIGURATION & API KEYS
# ==========================================
USER_AGENT = "MyMP3GenreTagger/1.0"
CONTACT_EMAIL = "your-email@example.com"

# >>> ADD YOUR API KEYS HERE (Keep blank if publishing publicly) <<<
DISCOGS_TOKEN = ""
GENIUS_CLIENT_ID = ""
GENIUS_CLIENT_SECRET = ""

# 1. Exact Match Aliases
GENRE_ALIASES = {
    "rnb": "R&B",
    "r&b/soul": "R&B",
    "funk / soul": "Soul",
    "stage & screen": "Soundtrack",
    "score": "Soundtrack",
    "ost": "Soundtrack",
    "electronica": "Electronic",
    "edm": "Electronic",
    "idm": "Electronic",
    "dnb": "Electronic"
}

# 2. Broad Regex Consolidations (Deep Taxonomy)
GENRE_REGEXES = [
    (r"(?i)\b(rap|hip-?hop|boom bap|lo-?fi hip-?hop|trap|grime|drill|crunk|turntablism|g-funk|cloud rap|mumble rap|chopped and screwed|bounce|conscious hip-?hop|abstract hip-?hop)\b", "Hip Hop"),
    (r"(?i)\b(r&b|rhythm and blues|neo soul|funk|soul|motown|disco|doo-?wop|quiet storm|p-funk|boogie|new jack swing)\b", "R&B"),
    (r"(?i)\b(alt(?:ernative)? rock|indie rock|grunge|shoegaze|post-rock|britpop|math rock|noise rock|slowcore|post-punk|dream pop|goth rock)\b", "Alternative Rock"),
    (r"(?i)\b(rock & roll|rock and roll|classic rock|hard rock|prog(?:ressive)? rock|psych(?:edelic)? rock|blues rock|surf rock|arena rock|soft rock|heartland rock|yacht rock|southern rock)\b", "Rock"),
    (r"(?i)\b(techno|house|trance|dubstep|idm|drum and bass|dnb|jungle|ambient|synthwave|chillout|electro|breakbeat|downtempo|trip-?hop|uk garage|chillwave|vaporwave|hardstyle|gabber|ebm|industrial|darkwave)\b", "Electronic"),
    (r"(?i)\b(death metal|black metal|thrash metal|heavy metal|doom metal|metalcore|djent|nu metal|grindcore|sludge metal|groove metal|symphonic metal|power metal|stoner metal|metal)\b", "Metal"),
    (r"(?i)\b(post-hardcore|hardcore|screamo|punk rock|pop punk|skate punk|garage punk|emo|crust|psychobilly|riot grrrl|ska punk|anarcho-punk|punk)\b", "Punk"),
    (r"(?i)\b(reggae|ska|dancehall|dub|rocksteady|roots reggae|lovers rock)\b", "Reggae"),
    (r"(?i)\b(indie pop|synthpop|electropop|k-pop|j-pop|dance-pop|art pop|chamber pop|hyperpop|bubblegum pop|city pop|sophisti-pop)\b", "Pop"),
    (r"(?i)\b(bluegrass|americana|country|honky tonk|alt-country|outlaw country|nashville sound)\b", "Country"),
    (r"(?i)\b(jazz|bebop|swing|bossa nova|hard bop|free jazz|jazz fusion|cool jazz|smooth jazz|dixieland|acid jazz|gypsy jazz)\b", "Jazz"),
    (r"(?i)\b(folk|acoustic|singer-songwriter|anti-folk|neo-folk|freak folk|indie folk|traditional folk)\b", "Folk"),
    (r"(?i)\b(delta blues|chicago blues|electric blues|piedmont blues|jump blues|boogie-woogie|blues)\b", "Blues"),
    (r"(?i)\b(reggaeton|salsa|bachata|cumbia|merengue|tejano|latin pop|norteño|mariachi|tango|ranchera|latin rock|rumba)\b", "Latin"),
    (r"(?i)\b(classical|baroque|romantic|choral|orchestral|symphony|opera|chamber|modern classical|minimalist)\b", "Classical"),
    (r"(?i)\b(afrobeat|highlife|kwaito|bollywood|celtic|worldbeat|fado|flamenco|klezmer|qawwali|zouk)\b", "World"),
    (r"(?i)\b(soundtrack|film score|motion picture|video game|anime theme|musical|broadway)\b", "Soundtrack"),
    (r"(?i)\b(comedy|stand-?up|novelty|parody|spoken word)\b", "Comedy")
]

def timed_prompt(prompt_text, timeout=5):
    """Cross-platform 5-second non-blocking input prompt."""
    sys.stdout.write(prompt_text)
    sys.stdout.flush()
    
    if sys.platform == "win32":
        import msvcrt
        start_time = time.time()
        ans = ""
        while time.time() - start_time < timeout:
            if msvcrt.kbhit():
                char = msvcrt.getwche()
                if char in ('\r', '\n'):
                    print()
                    return ans.lower().strip()
                ans += char
                sys.stdout.write(char)
                sys.stdout.flush()
            time.sleep(0.05)
        print()
        return ""
    else:
        import select
        i, o, e = select.select([sys.stdin], [], [], timeout)
        if i:
            return sys.stdin.readline().strip().lower()
        print()
        return ""

class GenreFinder:
    def __init__(self):
        self.session = requests.Session()
        musicbrainzngs.set_useragent(USER_AGENT, "1.0", CONTACT_EMAIL)
        
        # Pre-compile regex for maximum speed
        self.regex_map = [(re.compile(pattern), master) for pattern, master in GENRE_REGEXES]
        self.genius_token = self._get_genius_token()

    def _get_genius_token(self):
        if not GENIUS_CLIENT_ID or not GENIUS_CLIENT_SECRET:
            return None
        try:
            resp = self.session.post(
                "https://api.genius.com/oauth/token",
                data={"grant_type": "client_credentials", "client_id": GENIUS_CLIENT_ID, "client_secret": GENIUS_CLIENT_SECRET},
                timeout=10
            )
            if resp.status_code == 200:
                return resp.json().get("access_token")
        except Exception: pass
        return None

    def _fetch(self, url, params=None, headers=None, as_json=True):
        try:
            resp = self.session.get(url, params=params, headers=headers, timeout=8)
            if resp.status_code == 200:
                return resp.json() if as_json else resp.text
        except Exception: pass
        return None

    def is_valid_genre(self, tag):
        if not tag: return False
        tag_lower = tag.lower().strip()
        invalid_kw = [
            'seen live', 'favorites', 'awesome', 'vinyl', 'cd', 'cassette', 
            'mp3', 'download', 'owned', 'my library', 'buy', '1001 albums', 
            'classic', 'favorite', 'best of', 'collection', 'masterpiece', 
            'non-music', 'various', 'various artists'
        ]
        return not any(kw in tag_lower for kw in invalid_kw) and not re.fullmatch(r'\d{4}', tag_lower)

    def normalize_tag(self, tag):
        clean_tag = tag.lower().strip()
        if clean_tag in GENRE_ALIASES:
            return GENRE_ALIASES[clean_tag]
        for compiled_reg, master_genre in self.regex_map:
            if compiled_reg.search(clean_tag):
                return master_genre
        return tag.title().strip()

    def search_theaudiodb(self, artist, album):
        if not artist: return None 
        data = self._fetch("https://www.theaudiodb.com/api/v1/json/123/search.php", params={"s": artist})
        if data and data.get("artists"):
            genre = data["artists"][0].get("strGenre")
            if self.is_valid_genre(genre): return genre
        return None

    def search_lastfm(self, artist, album):
        if not artist: return None 
        url = f"https://www.last.fm/music/{urllib.parse.quote(artist)}/{urllib.parse.quote(album)}" if album else f"https://www.last.fm/music/{urllib.parse.quote(artist)}"
        html = self._fetch(url, headers={"User-Agent": USER_AGENT}, as_json=False)
        if html:
            tags_section = BeautifulSoup(html, 'html.parser').find('ul', class_='tags-list')
            if tags_section:
                for t in tags_section.find_all('a'):
                    if self.is_valid_genre(t.text.strip()): return t.text.strip()
        return None

    def search_discogs(self, artist, album):
        if not DISCOGS_TOKEN: return None
        headers = {"User-Agent": USER_AGENT, "Authorization": f"Discogs token={DISCOGS_TOKEN}"}
        params = {"q": f"{artist} {album}".strip(), "per_page": 1, "type": "release" if album else "artist"}
        data = self._fetch("https://api.discogs.com/database/search", params=params, headers=headers)
        if data and data.get("results"):
            for g in data["results"][0].get("genre", []) + data["results"][0].get("style", []):
                if self.is_valid_genre(g): return g
        return None

    def search_itunes(self, artist, album):
        params = {"term": f"{artist} {album}".strip(), "entity": "album" if album else "musicArtist", "limit": 1}
        data = self._fetch("https://itunes.apple.com/search", params=params)
        if data and data.get("results"):
            genre = data["results"][0].get("primaryGenreName")
            if self.is_valid_genre(genre): return genre
        return None

    def search_musicbrainz(self, artist, album):
        try:
            if album:
                kwargs = {"release": album, "limit": 1}
                if artist: kwargs["artist"] = artist
                res = musicbrainzngs.search_releases(**kwargs).get('release-list', [])
                if not res: return None
                tags = musicbrainzngs.get_release_group_by_id(res[0]['release-group']['id'], includes=["tags"]).get('release-group', {}).get('tag-list', [])
            else:
                if not artist: return None
                res = musicbrainzngs.search_artists(artist=artist, limit=1).get('artist-list', [])
                if not res: return None
                tags = res[0].get('tag-list', [])
            
            if tags:
                tags.sort(key=lambda t: int(t.get('count', 0)), reverse=True)
                for t in tags:
                    if self.is_valid_genre(t['name']): return t['name']
        except Exception: pass
        return None

    def search_genius(self, artist, album):
        if not self.genius_token: return None
        data = self._fetch("https://api.genius.com/search", params={"q": f"{artist} {album}".strip()}, headers={"Authorization": f"Bearer {self.genius_token}"})
        if data and data.get("response", {}).get("hits"):
            song_url = data["response"]["hits"][0].get("result", {}).get("url")
            if song_url:
                html = self._fetch(song_url, headers={"User-Agent": USER_AGENT}, as_json=False)
                if html:
                    for t in BeautifulSoup(html, 'html.parser').find_all('a', href=re.compile(r'/tags/')):
                        if self.is_valid_genre(t.text.strip()): return t.text.strip()
        return None

    def find_genre(self, artist, album, require_consensus=True):
        candidates, poll_stats = [], []
        services = [
            ("TheAudioDB", self.search_theaudiodb, 0.2),
            ("Last.fm", self.search_lastfm, 0.2),
            ("Discogs", self.search_discogs, 0.5),
            ("iTunes", self.search_itunes, 0.2),
            ("MusicBrainz", self.search_musicbrainz, 1.0),
            ("Genius", self.search_genius, 1.0)
        ]
        
        for name, func, delay in services:
            print(f"  [>] Polling {name}...")
            start_t = time.time()
            res = func(artist, album)
            elapsed = time.time() - start_t
            
            if res:
                norm_res = self.normalize_tag(res)
                candidates.append(norm_res)
                poll_stats.append(f"{name}[{elapsed:.1f}s]={res}->{norm_res}")
            else:
                poll_stats.append(f"{name}[{elapsed:.1f}s]=None")
                
            if len(candidates) >= 2:
                counts = Counter(candidates)
                best_norm, count = counts.most_common(1)[0]
                if count >= 2:
                    return best_norm, f"Early Consensus (2 votes) | Stats: {', '.join(poll_stats)}"
            
            time.sleep(delay)
            
        if not candidates:
            return None, f"Stats: {', '.join(poll_stats)}"
            
        counts = Counter(candidates)
        best_norm, count = counts.most_common(1)[0]
        
        if require_consensus and count < 2:
            return None, f"No Consensus ({count}/{len(candidates)} votes) | Stats: {', '.join(poll_stats)}"
            
        return best_norm, f"Consensus ({count}/{len(candidates)} votes) | Stats: {', '.join(poll_stats)}"


class MP3TaggerApp:
    def __init__(self, target_dir):
        self.target_dir = target_dir
        self.log_file = os.path.join(target_dir, "tagger_progress.log")
        self.fail_file = os.path.join(target_dir, "failed_searches.txt")
        self.processed = self._load_history()
        self.artist_cache = {} 
        self.finder = GenreFinder()

    def _load_history(self):
        history = set()
        if os.path.exists(self.log_file):
            with open(self.log_file, 'r', encoding='utf-8') as f:
                for line in f:
                    history.add(line.strip().split(" | ")[0])
        return history

    def _mark_success(self, folder_path, genre, service, elapsed_time):
        with open(self.log_file, 'a', encoding='utf-8') as f:
            f.write(f"{folder_path} | Genre: {genre} | Service: {service} | Total Time: {elapsed_time:.1f}s\n")

    def _mark_failure(self, folder_path, artist, album, stats, elapsed_time):
        with open(self.fail_file, 'a', encoding='utf-8') as f:
            f.write(f"No genre found | Folder: {folder_path} | Searched: {artist} - {album} | {stats} | Total Time: {elapsed_time:.1f}s\n")

    def _clean_tag(self, audio_file, tag_name):
        try: return str(audio_file.get(tag_name, [''])[0]).strip()
        except Exception: return ""

    def run(self):
        for root, _, files in os.walk(self.target_dir):
            if root in self.processed: continue
            mp3s = [f for f in files if f.lower().endswith('.mp3')]
            if not mp3s: continue
                
            print(f"\n[SCAN] {root}")
            folder_start_time = time.time()
            raw_artist, raw_album, is_compilation = "", "", False
            
            for file in mp3s:
                path = os.path.join(root, file)
                try:
                    audio = EasyID3(path)
                    alb_artist = self._clean_tag(audio, 'albumartist')
                    trk_artist = self._clean_tag(audio, 'artist')
                    raw_artist = alb_artist if alb_artist else trk_artist
                    raw_album = self._clean_tag(audio, 'album')
                    
                    comp_flag = self._clean_tag(audio, 'compilation')
                    if comp_flag == '1':
                        is_compilation = True

                    if raw_artist and raw_album: break
                except Exception: continue
                    
            if not raw_artist or not raw_album:
                print("  [-] Skipped: No Artist/Album tags found.")
                continue
            
            primary_artist = re.split(r'(?i)\s+feat\.?\s+|\s+ft\.?\s+|\s+&\s+|\s+and\s+', raw_artist)[0].strip()
            if not primary_artist: primary_artist = raw_artist
                
            clean_album = re.sub(r'