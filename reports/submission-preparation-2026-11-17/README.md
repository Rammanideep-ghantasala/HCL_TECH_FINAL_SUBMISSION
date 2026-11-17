# Verification evidence — 17 November 2026

These results were recorded against the application from development commit `0acef1654e35a6184e284185e6ef99f5f07eeeb4` before packaging. The published application, tests, configuration, lockfiles and seed files are byte-for-byte copies of the inspected working tree. Packaging changed documentation and ignore rules only.

| Check | Result | Evidence |
| --- | --- | --- |
| Backend pytest | 164 passed, 14 PostgreSQL tests skipped (`TEST_DATABASE_URL` unset) | [JUnit](backend-tests.xml), [log](backend-tests.log) |
| Frontend Vitest | 71 passed across 7 files; jsdom and mocked API responses | [JUnit](frontend-tests.xml), [log](frontend-tests.log) |
| Frontend typecheck, lint, build | Exit status 0 for each; empty typecheck/lint logs indicate no diagnostics | [typecheck](frontend-typecheck.log), [lint](frontend-lint.log), [build](frontend-build.log) |
| Deployment configuration | 19 cases passed; no cloud deployment | [log](deployment-config.log) |
| Optional 1,000-ticket synthetic data | 16 structural checks passed, no automated content flags | [JSON](dataset-validation.json), [log](dataset-validation.log) |
| Frozen final retrieval set | 24 queries, original 40-ticket corpus; hybrid main evidence 5/12, top-three ranking 10/12, false close matches 0/11 | [JSON](retrieval-final.json), [log](retrieval-final.log) |
| Additional safety probes | 7 scenarios; 3 pass and 4 expose policy/capacity/freshness gaps | [JSON](safety-probes.json), [log](safety-probes.log), [script](verify_submission.py) |

The four safety gaps are unrelated revision-checked replacement, saving before a failed embedding rebuild, retaining stale search results when every source record is removed, and the unenforced saved-ticket capacity. They remain documented limitations. The probe script reproduces existing behavior on temporary synthetic records; it is not a production-readiness certification.

Reproduce the application tests and retrieval evaluation using the root README. Prepare the real embedding model first to exercise the model-dependent tests and safety script. Run the safety script from the repository root with `backend/.venv/Scripts/python.exe reports/submission-preparation-2026-11-17/verify_submission.py` on Windows (use `backend/.venv/bin/python` on Linux/macOS). It refreshes the safety evidence outputs and does not modify the original seed. Run configuration checks with `powershell -NoProfile -File deploy/aws/tests/Test-DeployConfig.ps1`.

JUnit machine hostnames and absolute project paths were removed from the publication copies. Test names, outcomes, counts and timings were preserved. Measurements are local synthetic samples; current PostgreSQL integration, live LLM quality, cloud deployment and sustained production load were not verified in this evidence run.
