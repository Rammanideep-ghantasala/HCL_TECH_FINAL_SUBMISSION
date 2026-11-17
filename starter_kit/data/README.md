# Synthetic ticket data

- `tickets.json`: the original 40-ticket case-study seed, used by default.
- `new_tickets_example.json`: the supplied incoming-ticket examples.
- `tickets_1000.json`: an optional synthetic expansion, including the original 40 records unchanged and 960 fictional incidents. It is included to reproduce the structural validation reported in the technical PDF; it is not the corpus used for the final retrieval measurements.

All records are synthetic. The expanded data does not establish real customer accuracy, and automatic structure checks do not prove that every resolution is technically correct. Adding records changes TF-IDF weights and retrieval decisions; measure before using the expansion as the retrieval corpus.

From the repository root, run `python tools/validate_dataset.py`. This uses the standard library and writes `reports/validation_results.json`. The included evidence snapshot contains the 16 passing structural checks and content flags from 17 November 2026.

For a focused TalentStudio ZIP, retain the two small seed/example files required by the implementation; omit the optional expansion and its validation artifacts. GitHub contains the optional data for reproducibility.
