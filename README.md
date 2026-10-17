# GenreShark

Auto-consensus genre tagger for local audio libraries. For each album folder it asks up to six
music databases what genre the release is, lets them vote, maps the winner onto one of 28 standard
genres, and writes it to the genre tag. It can preview everything first (`--dry-run`) and roll back
any run (`--undo`).

## Features

- **Multi-source voting.** Discogs, Last.fm, iTunes, TheAudioDB, Genius and MusicBrainz. Each
  source votes with its top tags, and a genre is only written when two different sources agree.
  (Compilations and artist-only fallbacks accept a single source, but only if it maps to a known genre.)
- **Fast by design.** Discogs and Last.fm are asked first, in parallel. The other four sources are
  only polled if those two don't agree, and the first agreement ends the lookup.
- **Clean taxonomy.** About 280 keyword patterns and aliases (shoegaze, math rock, djent,
  synthwave, ...) are consolidated into 28 genres. The longest match wins, so "Industrial Metal" is
  Metal and "Folk Rock" stays Folk.
- **Hit verification.** A search result must fuzzily match your artist and album before its tags
  are trusted, and numbers in titles must agree ("Now 45" will not match "Now 54").
- **Compilations.** "Various Artists" releases are detected from the folder name or the
  compilation flag and looked up by album title.
- **Real band names.** "Hall & Oates", "Earth, Wind & Fire" and "AC/DC" are searched as written;
  the first-listed artist is only a strict-match fallback for collaborations.
- **Many formats.** MP3, FLAC, M4A/MP4, Ogg/Opus, WMA, AIFF, WAV, DSF and more (see below).
- **Resumable.** Finished folders are remembered, and artist genres are cached between runs.
- **Dry run + CSV report.** See every proposed change before touching a file.
- **Undo.** Every tag change is logged with the genre it replaced.
- **Safe writes.** ID3v2.3 files stay v2.3, and files without any tag get one.

## Supported formats

Anything [Mutagen](https://mutagen.readthedocs.io/) can tag:

`.mp3` `.flac` `.ogg` `.oga` `.opus` `.spx` `.m4a` `.m4b` `.m4p` `.mp4` `.wma` `.asf` `.ape` `.wv`
`.mpc` `.tta` `.aiff` `.aif` `.aifc` `.wav` `.dsf` `.dff` `.ofr` `.ofs` `.tak`

A file that can't be read or written is reported and its folder is retried on the next run.

## Install

Python 3.9 or newer.

```
pip install -r requirements.txt
```

This installs `requests`, `musicbrainzngs` and `mutagen`. `beautifulsoup4` is optional: it is only
needed for Genius and for Last.fm when you have no Last.fm API key.

## Configure

Settings live in `config.json` next to `GenreShark.py`. It is created as an empty template on the
first real run, or copy `config.example.json`. Environment variables with the same names work as a
fallback; a value in `config.json` wins, and empty values never win.

| Key                                        | Needed for                                                                                            |
| ------------------------------------------ | ----------------------------------------------------------------------------------------------------- |
| `CONTACT_EMAIL`                            | Identifies you to MusicBrainz (use a real address)                                                    |
| `DISCOGS_TOKEN`                            | Discogs (personal access token from your Discogs developer settings)                                  |
| `GENIUS_CLIENT_ID`, `GENIUS_CLIENT_SECRET` | Genius (also needs `beautifulsoup4`)                                                                  |
| `LASTFM_API_KEY`                           | Official Last.fm API (optional; without it the Last.fm page is scraped, which needs `beautifulsoup4`) |

iTunes, TheAudioDB and MusicBrainz need no keys. Sources without credentials are simply skipped.

> **Keep your keys private.** `config.json` holds secrets. If you fork or clone this repo, make sure
> `config.json` is in your `.gitignore` before you fill it in.

## Usage

```
python GenreShark.py "D:\Music" --dry-run     # 1. preview: writes only a CSV report
python GenreShark.py "D:\Music"               # 2. tag the library
python GenreShark.py "D:\Music" --undo        # 3. revert the most recent run
```

If you leave out the path you will be prompted for it.

| Option                           | What it does                                                                                                                                  |
| -------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `--dry-run`                      | Look up genres and write `genre_dry_run_<id>.csv` listing every proposed change. Changes nothing else and never prompts.                      |
| `--dry-run-cached`               | Like `--dry-run`, but also saves resolved artists to `artist_cache.json`, so a later real run (or dry run) reuses those lookups.              |
| `--report FILE.csv`              | Write the same CSV during a real run.                                                                                                         |
| `--only-untagged`                | Only tag files with no genre. Fully tagged folders are skipped without any network calls.                                                     |
| `--overwrite` / `--no-overwrite` | Answer the "replace existing genre?" question up front.                                                                                       |
| `--reprocess`                    | Ignore saved progress and rescan every folder. (The artist cache is still used; delete `artist_cache.json` to force fresh artist lookups.)    |
| `--services a,b`                 | Use only some sources: `discogs,lastfm,itunes,theaudiodb,genius,musicbrainz`.                                                                 |
| `--undo`                         | Revert the most recent run (add `--dry-run` to preview).                                                                                      |
| `--undo-run RUN_ID` / `all`      | Revert one specific run, or every run.                                                                                                        |
| `--list-runs`                    | Show logged runs that can still be undone.                                                                                                    |
| `--force`                        | With `--undo`: restore even if you edited the genre after GenreShark tagged it.                                                               |
| `-v`                             | Show request errors and debug detail.                                                                                                         |
| `--selftest`                     | Offline sanity checks for the taxonomy and helpers. Exits non-zero on failure.                                                                |

When a folder already has different genres, GenreShark asks once per folder
(`yes / no / all folders / skip all`, 5 seconds, default no).

The dry-run CSV has the columns `mode, folder, file, artist, album, old_genre, new_genre, action, source`.
It is written with a UTF-8 BOM so Excel opens it correctly.

## How it works

1. **Read the folder.** The artist and album come from the first file that has both tags (album
   artist is preferred over artist). The whole folder is treated as one album. A folder is a
   compilation if its name looks like "Various Artists" / "VA -", or any file has the compilation flag.
2. **Poll the sources.** Each source returns its top tags. Tags are cleaned (things like "seen live",
   "favorites" and decades are dropped), normalized to a master genre, and each source's top three
   genres score 1, 1/2 and 1/3 points.
3. **Vote.** As soon as one genre is backed by two different sources, it wins. Ties are broken by
   score, then number of sources, then source order, so the result is the same on every run.
4. **Fall back if needed.**
   - *Normal albums:* artist + cleaned album title, then the original title, then the first-listed
     artist (only on a ≥ 0.95 name match), then an artist-only lookup (a single source is accepted).
   - *Compilations:* album title alone, then "Various Artists" + title, then the label/folder artist
     (a single source is accepted).
5. **Write.** The genre is written to every readable file in the folder, and each change is logged for undo.

Lookups that fail cleanly (no source knew the release) are remembered for the rest of the run, so the
same artist is not searched twice. Failures caused by network or HTTP errors are never remembered.

Requests are throttled per host: Discogs 1/s, iTunes one request per 3 s (Apple documents about 20 per
minute), Genius and TheAudioDB about 2/s, Last.fm up to 4/s with the API and 2/s when scraped.
MusicBrainz is rate-limited by its client library.

## Files it writes (in your music folder)

| File                     | Purpose                                                           |
| ------------------------ | ----------------------------------------------------------------- |
| `genre_undo.jsonl`       | Every tag change with the previous value, used by `--undo`        |
| `tagger_state.jsonl`     | Per-folder results, so finished folders are skipped next time     |
| `artist_cache.json`      | Artist to genre cache (versioned)                                 |
| `failed_searches.txt`    | Folders where no genre could be found                             |
| `genre_dry_run_<id>.csv` | The report a dry run produces (the only thing a dry run writes)   |

A dry run does not write the state file, the undo log, or (unless you use `--dry-run-cached`) the cache.

Folders that were tagged, already matched, or where you declined an overwrite are skipped on the next
run. Folders with no result, with errors, or with no artist/album tags are retried every run.

## Undo

`--undo` restores the exact previous genre (or removes the genre tag if there was none). If you have
changed a file's genre since GenreShark tagged it, that file is left alone unless you pass `--force`.
Use `--list-runs` to see run IDs, `--undo-run RUN_ID` for a specific run, and add `--dry-run` to preview.

Only the genre field is logged and restored. GenreShark edits tags in place and does not make backup
copies of your files.

## Customizing the genres

Edit `GENRE_ALIASES`, `GENRE_REGEXES` and `MASTER_GENRES` near the top of `GenreShark.py`, then:

1. Run `python GenreShark.py --selftest`.
2. Bump `TAXONOMY_VERSION` so finished folders and cached artists are re-evaluated with the new rules.

Some mappings worth knowing: funk and disco map to Soul, "Country Rock" is Rock, "Jazz Rap" and
"Hardcore Hip Hop" are Hip Hop, "Latin Jazz" is Latin, and "Stage & Screen" is Soundtrack.

The complete list of **Master Genres**:

Alternative Rock, Blues, Children's, Classical, Comedy, Country, Dancehall, Dub, Electronic, Folk,
Gospel, Hip Hop, Holiday, Jazz, Latin, Metal, New Age, Pop, Punk, R&B, Reggae, Rock, Rockabilly,
Rocksteady, Ska, Soul, Soundtrack, World.

## Limitations

- **One genre per artist after the first hit.** Once an album by an artist resolves, that genre is
  cached and reused for the artist's other albums, which can be wrong for artists who change style.
  Delete `artist_cache.json` (or the artist's entry) to look them up again.
- **One album per folder.** Mixed folders (a "Singles" or "Downloads" folder) are tagged from the
  first file's artist and album, so keep albums in their own folders.
- **Single genre written.** Only the first existing genre value is read, and an overwrite replaces all
  genre values on the file.
- **M4A:** only the standard genre atom is read; files that store an iTunes numeric genre may look untagged.
- Without a Last.fm API key and Genius credentials, tags are read from web pages, which can break
  if those sites change.
- A first pass over a large library takes a while because of the per-host limits; later runs use the cache.
- Genre data is community-sourced. Use `--dry-run` first and review the report.

## License

This project is licensed under the MIT License.
You are free to use, modify, and distribute this software. Attribution appreciated but not required.
