# GenreShark

Auto-consensus genre tagger for MP3 libraries. For each album folder it asks up to six music
databases what genre the release is, lets them vote, maps the winner onto one of 28 standard
genres, and writes it to the ID3 genre tag. It can preview everything first (`--dry-run`) and
roll back any run (`--undo`).

## Features

- **Multi-source voting.** Discogs, Last.fm, iTunes, TheAudioDB, Genius and MusicBrainz. Each
  source votes with its top tags, and a genre is only written when two different sources agree.
  (Compilations and artist-only fallbacks accept a single source, but only if it maps to a known genre.)
- **Parallel lookups.** Discogs and Last.fm are asked first, in parallel; the other sources are only
  polled if those two don't agree.
- **Clean taxonomy.** About 240 keyword patterns (shoegaze, math rock, djent, synthwave, ...) are
  consolidated into 27 genres. The longest match wins, so "Industrial Metal" is Metal and
  "Folk Rock" stays Folk.
- **Hit verification.** A search result must fuzzily match your artist and album before its tags
  are trusted, so a wrong album on a database can't vote.
- **Compilations.** "Various Artists" releases are detected from the folder name or the ID3
  compilation flag and looked up by album title.
- **Real band names.** "Hall & Oates", "Earth, Wind & Fire" and "AC/DC" are searched as written;
  the first-listed artist is only a fallback for collaborations.
- **Resumable.** Finished folders are remembered, and artist genres are cached between runs.
- **Dry run + CSV report.** See every proposed change before touching a file.
- **Undo.** Every tag change is logged with the genre it replaced.
- **Safe writes.** ID3v2.3 files stay v2.3, and files without any tag get one.

The 27 genres: Hip Hop, R&B, Soul, Alternative Rock, Rock, Electronic, Metal, Punk, Reggae, Ska,
Dancehall, Dub, Rocksteady, Pop, Country, Jazz, Folk, Blues, Latin, Classical, World, Soundtrack,
Comedy, Gospel, Holiday, Children's, New Age.

## Install

Python 3.9 or newer.

```
pip install -r requirements.txt
```

## Configure

Settings live in `config.json` next to `GenreShark.py`. It is created as an empty template on the
first real run, or copy `config.example.json`. Environment variables with the same names work as a
fallback; a value in `config.json` wins, and empty values never win.

| Key | Needed for |
|---|---|
| `CONTACT_EMAIL` | Identifies you to MusicBrainz (use a real address) |
| `DISCOGS_TOKEN` | Discogs (personal access token from your Discogs developer settings) |
| `GENIUS_CLIENT_ID`, `GENIUS_CLIENT_SECRET` | Genius (also needs `beautifulsoup4`) |
| `LASTFM_API_KEY` | Official Last.fm API (optional; without it the Last.fm page is scraped, which needs `beautifulsoup4`) |

iTunes, TheAudioDB and MusicBrainz need no keys. Sources without credentials are simply skipped.

**Never commit `config.json`.** It is listed in `.gitignore`.

## Usage

```
python GenreShark.py "D:\Music" --dry-run     # 1. preview: writes only a CSV report
python GenreShark.py "D:\Music"               # 2. tag the library
python GenreShark.py "D:\Music" --undo        # 3. revert the most recent run
```

| Option | What it does |
|---|---|
| `--dry-run` | Look up genres and write `genre_dry_run_<id>.csv` listing every proposed change. Changes nothing else and never prompts. |
| `--dry-run-cached` | Look up genres and write `genre_dry_run_<id>.csv` allows `artist_cache.json` to be read from and written to, so any resolved artist lookups during your dry run are safely cached. |
| `--report FILE.csv` | Write the same CSV during a real run. |
| `--only-untagged` | Only tag files with no genre. Fully tagged folders are skipped without any network calls. |
| `--overwrite` / `--no-overwrite` | Answer the "replace existing genre?" question up front. |
| `--reprocess` | Ignore saved progress and rescan every folder. |
| `--services a,b` | Use only some sources: `discogs,lastfm,itunes,theaudiodb,genius,musicbrainz`. |
| `--undo` | Revert the most recent run (add `--dry-run` to preview). |
| `--undo-run RUN_ID` / `all` | Revert one specific run, or every run. |
| `--list-runs` | Show logged runs that can still be undone. |
| `--force` | With `--undo`: restore even if you edited the genre after GenreShark tagged it. |
| `-v` | Show request errors and debug detail. |
| `--selftest` | Offline sanity checks for the taxonomy and helpers. |

When a folder already has different genres, GenreShark asks once per folder
(`yes / no / all folders / skip all`, 5 seconds, default no).

## Files it writes (in your music folder)

| File | Purpose |
|---|---|
| `genre_undo.jsonl` | Every tag change with the previous value, used by `--undo` |
| `tagger_state.jsonl` | Per-folder results, so finished folders are skipped next time |
| `artist_cache.json` | Artist to genre cache (versioned) |
| `failed_searches.txt` | Folders where no genre could be found |
| `genre_dry_run_<id>.csv` | The report a dry run produces (the only thing a dry run writes) |

`--undo` restores the exact previous genre (or removes the genre tag if there was none). If you have
changed a file's genre since GenreShark tagged it, that file is left alone unless you pass `--force`.
Only changes made by this version can be undone.

## Customizing the genres

Edit `GENRE_ALIASES`, `GENRE_REGEXES` and `MASTER_GENRES` near the top of `GenreShark.py`, then:

1. Run `python GenreShark.py --selftest`.
2. Bump `TAXONOMY_VERSION` so finished folders and cached artists are re-evaluated with the new rules.

## Limitations

- MP3 (ID3) only.
- Without a Last.fm API key, and for Genius, tags are read from web pages, which can break if those
  sites change.
- Requests are throttled per host (Apple documents about 20 iTunes requests per minute), so a first pass over a
  large library takes a while. Later runs use the cache.
- Genre data is community-sourced. Use `--dry-run` first and review the report.

License
This project is licensed under the MIT License.
You are free to use, modify, and distribute this software. Attribution appreciated but not required.
