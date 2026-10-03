"""
Download NVBW's timetable file and build the stop database from it.

The app needs this database for the map and the "stops in this area"
endpoint. Run it once after cloning the project, and again whenever you
want fresher data (NVBW publishes a new file twice a month). It is safe to
run while the app is running.

Usage:
    python import_stops.py                  # download (about 55 MB) and build
    python import_stops.py --file some.zip  # build from a file you already have
    python import_stops.py --keep           # keep the downloaded file afterwards
    python import_stops.py --force          # import even if the file looks incomplete

Result: data/stops.sqlite (not in git).
Data: "Datensatz der NVBW GmbH", https://www.nvbw.de/open-data
"""
import argparse
import sqlite3
import sys
import time
from pathlib import Path

import requests

from gtfs import importer, store

DOWNLOAD_PATH = store.DB_PATH.parent / "bwgesamt.zip"


def show_progress(done: int, total: int) -> None:
    size = f"{done / 1e6:.0f} of {total / 1e6:.0f} MB" if total else f"{done / 1e6:.0f} MB"
    print(f"\r  downloaded {size}", end="", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", type=Path, help="use this GTFS zip instead of downloading")
    parser.add_argument("--keep", action="store_true", help="keep the downloaded file")
    parser.add_argument("--force", action="store_true", help="import even if the file holds suspiciously few stations")
    args = parser.parse_args()

    downloaded = not args.file
    zip_path = args.file
    if zip_path:
        if not zip_path.exists():
            print(f"File not found: {zip_path}")
            return 1
    else:
        zip_path = DOWNLOAD_PATH
        print(f"Downloading {importer.SOURCE_URL}")
        try:
            importer.download(zip_path, progress=show_progress)
        except requests.RequestException as e:
            print(f"\nDownload failed: {e}\nCheck your internet connection and try again. The existing database was not changed.")
            return 1
        except importer.BadFeed as e:
            print(f"\nDownload failed: {e}\nThe existing database was not changed.")
            return 1
        print()

    print("Building the stop database (takes about half a minute)...")
    started = time.time()
    try:
        meta = importer.build(zip_path, store.DB_PATH, force=args.force)
    except importer.BadFeed as e:
        print(f"Import failed: {e}")
        return 1
    except sqlite3.OperationalError as e:
        print(f"Import failed: the database is busy or can't be written ({e}). Try again in a moment. "
              "The existing database was not changed.")
        return 1
    except OSError as e:
        print(f"Import failed: {e}")
        return 1
    finally:
        if downloaded and not args.keep:
            zip_path.unlink(missing_ok=True)

    print(f"  {meta['stations']} stations in Baden-Württemberg, timetable version {meta['feed_version'] or '?'} "
          f"(valid {meta['feed_start_date']} to {meta['feed_end_date']})")
    print(f"  duplicates: {meta['duplicates']}")
    print(f"  written to {store.DB_PATH} in {time.time() - started:.0f} s")
    if store.info()["expired"]:
        print("  Note: this timetable period has already ended. NVBW may not have published a newer file yet; "
              "run the import again later.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
