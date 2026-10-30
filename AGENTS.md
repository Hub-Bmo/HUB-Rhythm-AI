# Research handoff instructions

Read `README.md`, `AI_HANDOFF.md`, and `docs/REPRODUCE.md` before work.

Preserve archived data, models and experiment logs. Write new results to `reproduced_runs/`, regenerated data to `work/`, and diagnostics to `verification/`. Never relabel unknown musical fields as known or merge simultaneous projected events by default.

V2 and V3 are independent fine-tuning branches from V1. Keep group-level splits and target masks. Treat historical paths as provenance, resolve packaged paths, and do not silently access the original author's computer directories.

Check `python reproduce.py verify --models` and `python reproduce.py test` before relying on the package. Distinguish stored results, freshly reproduced results, and untested free-generation claims.
