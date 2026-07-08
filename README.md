# VHYes

VHYes is a home media catalog for physical collections.

The goal is to make a local network app that feels like browsing a personal streaming service, while still tracking the real physical media on the shelf.

## Core Concept

- Add media by title search or barcode scan.
- Track the physical format: VHS, DVD, Blu-ray, 4K, and other formats.
- Store records in a local database instead of CSV/JSON files.
- Browse the library in non-traditional ways, including rating, mood, year, genre, format, watch status, and collection shelves.
- Run locally on the home network so phones, tablets, and TVs can browse the collection.
- Preserve poster/cover images locally when licensing and source terms allow it; otherwise store remote image URLs and metadata.

## Legacy Code

The original prototype has been moved to `OLD/`. It was a Flask app using CSV and JSON files, with IMDb title search for movie adds and a separate book flow.

## New Version Direction

The next version should start with a small local database-backed app. A practical first pass:

- SQLite for local storage.
- Tables for media items, physical copies, people/credits, genres, moods, barcodes, and images.
- A scanner-friendly add screen that accepts UPC/EAN barcodes and manual title search.
- A browse screen designed around discovery filters rather than only alphabetic lists.
- Local image cache with source attribution fields.
