# Submission checklist

Run in this order; do the freshness-sensitive Jobs and News collection last, within the same hour as submission.

1. [ ] Configure `.env` and verify API/service-account access without committing secrets.
2. [ ] Run papers: `python -m src.main papers --limit 1000`.
3. [ ] Run startups: `python -m src.main startups --limit 1000`.
4. [ ] Run products: `python -m src.main products --limit 1000`.
5. [ ] Pricing enrichment remains a standalone sample only: `python -m src.llm.orchestrator data\pricing_sample.txt`. Product pricing stays null unless supported by evidence.
6. [ ] Resolve entities: `python -m src.main resolve`.
7. [ ] Run fresh Jobs collection: `python -m src.main jobs`.
8. [ ] Run fresh News collection immediately after Jobs and within the same hour as submission: `python -m src.main news`.
9. [ ] Export: `python -m src.main export`; run `.\scripts\audit.ps1` and resolve every FAIL before submitting. `output/jobs.jsonl` and `output/news.jsonl` must exist and meet their row/freshness checks.
10. [ ] Commit and push the verified submission; set the Sheet to **Anyone with the link — Viewer**; open it in an Incognito window to verify access; submit the form.
