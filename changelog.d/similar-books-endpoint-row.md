### Fixed

- **"Books like this" now appears on book pages.** The suggestions request failed with a server error on every book, so the strip never showed. It now reads the book's Hardcover ID correctly and loads.
- **"Books like this" suggests books that actually fit.** Suggestions now match on all of a book's Hardcover genres and its moods, skip titles almost nobody has read, and no longer latch onto one stray tag (a dystopian novel tagged "Forgiveness" was getting forgiveness memoirs).
