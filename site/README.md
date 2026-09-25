# ebookerr plugins

20 plugins — generated from the released packages by `python -m ebookerr_sdk.pack index`; do not edit by hand.

| Plugin | Id | Kind | Version | Summary |
|---|---|---|---|---|
| DOCX download | `docx_download_source` | Source | 1.1.0 | Downloads a DOCX and converts it to EPUB in the library. |
| Chapter Reorder | `epub_chapter_reorder` | EPUB | 2.1.0 | Puts an EPUB's chapters back into numbered order when the source delivered them shuffled. |
| Chapter URL Stamp | `epub_chapter_url` | EPUB | 1.1.0 | Records each chapter's own web address inside the EPUB, so a chapter can be traced back to the page it came from. |
| EPUB download | `epub_download_source` | Source | 1.1.0 | Downloads a story EPUB from a direct download URL. |
| EPUB Merge | `epub_merge` | EPUB | 2.1.0 | Combines several books into one, moving every chapter into the first book and deleting the others. |
| EPUB Normalize | `epub_normalize` | EPUB | 1.1.0 | Strip publisher styling that stops your reader applying its own fonts, colours, spacing and margins. |
| EPUB Validate | `epub_validate` | EPUB | 1.1.0 | Check every EPUB ebookerr writes for structural problems, and report what it finds without ever changing the file. |
| FanFicFare | `fanficfare_source` | Source | 1.1.0 | Downloads stories from any site FanFicFare supports. |
| File metadata sync | `file_meta_sync` | Book | 2.1.0 | Syncs sidecar files (cover candidates, synopsis) with the library |
| Kavita Sync | `kavita_sync` | Book | 1.1.0 | Publishes your books to a Kavita server and reads your reading progress back. |
| Komga Sync | `komga_sync` | Book | 1.1.0 | Publishes your books to a Komga server and reads your reading progress back. |
| Literotica search | `literotica_stories` | Catalog | 2.2.0 | Fetches stories with Literotica search |
| Cover generator | `llm_cover` | Book | 1.1.0 | Creates candidate covers for a book with image models you run or subscribe to: a language model first writes an image prompt from the opening chapters. |
| Synopsis generator | `llm_synopsis` | Book | 1.1.0 | Writes a book's synopsis from its opening chapters with a language model you run or subscribe to. |
| Literotica - My Home | `my_literotica` | Catalog | 1.1.0 | Lists new story publications from the authors you follow on Literotica (from "My Home" activity wall) |
| Patreon memberships | `patreon_stories` | Catalog | 2.2.0 | Scans every Patreon membership you hold — paid, cancelled or free — for posts you can open that carry a story file |
| PDF download | `pdf_download_source` | Source | 1.1.0 | Downloads a PDF and converts it to EPUB in the library. |
| RTF download | `rtf_download_source` | Source | 1.1.0 | Downloads an RTF and converts it to EPUB in the library. |
| Text download | `text_download_source` | Source | 1.1.0 | Downloads a TXT/Markdown file and converts it to EPUB in the library. |
| URL Story Extractor | `url_story_extractor` | Catalog | 1.1.0 | Extracts the stories listed on any page — an author's works, a series, a favourites list — using FanFicFare's site adapters, falling back to generic link scraping. |
