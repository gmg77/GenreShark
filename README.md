# Auto-Consensus MP3 Genre Tagger

A high-speed, highly automated Python script that tags local MP3 files with accurate genre metadata. Instead of relying on a single source, this tool polls multiple community-curated databases and applies a strict voting consensus to guarantee the most accurate, standardized tag possible.

## How It Works
1. **Multi-Source Polling:** Queries TheAudioDB, Last.fm, Discogs, iTunes, MusicBrainz, and Genius API simultaneously.
2. **Early Consensus:** To maximize speed, if the first two databases agree on a genre, the script stops polling and instantly writes the tag.
3. **Advanced Taxonomy Normalization:** Raw database tags are aggressively filtered through a pre-compiled Regex engine. Over 150 obscure micro-genres (e.g., *shoegaze*, *math rock*, *djent*, *synthwave*) are automatically consolidated into 18 clean, standardized Master Genres.
4. **Intelligent Compilation Handling:** Automatically detects "Various Artists" compilations (via name or ID3 flags), suspends strict consensus rules, and evaluates the release based strictly on the album title.
5. **Artist Caching:** Remembers genre consensus per artist during a scan, allowing full discographies to be tagged in milliseconds after the first album is resolved.

## Requirements
* `requests`
* `beautifulsoup4`
* `mutagen`
* `musicbrainzngs`

## Usage
Run the script and provide the absolute path to your root music directory. The script will recursively scan all subfolders, skip already correctly-tagged files, and prompt for batch overwrites when conflicting legacy tags are found.
