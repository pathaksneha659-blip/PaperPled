import asyncio
import json
import re
from datetime import date, datetime, timedelta, timezone
import xml.etree.ElementTree as ET
from pathlib import Path
from src.core.http import HttpClient
from src.schemas import ResearchPaperRecord, ResearchPaperContent, SourceInfo
from src.config import settings
from src.logging_setup import log

GH_REPO_REGEX = re.compile(r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", re.I)
RESERVED_ORGS = {"features", "topics", "sponsors", "marketplace", "about", "collections", "login", "pricing"}

def clean_github_repo(url: str | None) -> tuple[str, str] | None:
    if not url:
        return None
    match = GH_REPO_REGEX.search(url)
    if not match:
        return None
    owner = match.group(1).lower()
    repo = re.sub(r"(\.git|[.,;:)\]]+)$", "", match.group(2)).lower()
    if owner in RESERVED_ORGS or not repo:
        return None
    return owner, repo

async def fetch_hf_daily_papers(http: HttpClient, days_back: int = 120) -> dict[str, dict]:
    """Collects arXiv IDs and linked GitHub repositories from Hugging Face Daily Papers."""
    log.info("papers.hf_fetch_start", days_back=days_back)
    today = date.today()
    papers_by_arxiv_id = {}

    for i in range(days_back):
        d_str = (today - timedelta(days=i)).isoformat()
        url = f"https://huggingface.co/api/daily_papers?date={d_str}"
        try:
            items = await http.get(url, as_json=True)
            for it in (items or []):
                p = it.get("paper", it)
                aid = p.get("id")
                if aid:
                    gh_link = None
                    if "githubRepo" in p and p["githubRepo"]:
                        gh_link = f"https://github.com/{p['githubRepo']}"
                    papers_by_arxiv_id[aid] = {
                        "id": aid,
                        "title": p.get("title", "").strip(),
                        "github_url": gh_link,
                        "published_at": p.get("publishedAt", "")
                    }
        except Exception as e:
            log.warning("papers.hf_date_error", date=d_str, error=str(e))

    log.info("papers.hf_fetch_complete", unique_papers_found=len(papers_by_arxiv_id))
    return papers_by_arxiv_id

async def enrich_via_arxiv_api(http: HttpClient, papers_dict: dict[str, dict], limit: int = 1500):
    """Enriches papers with authoritative author names and publication dates from arXiv API."""
    arxiv_ids = list(papers_dict.keys())[:limit]
    log.info("papers.arxiv_enrichment_start", target_count=len(arxiv_ids))

    for i in range(0, len(arxiv_ids), 100):
        batch = arxiv_ids[i:i + 100]
        url = f"http://export.arxiv.org/api/query?id_list={','.join(batch)}&max_results=100"
        try:
            xml_text = await http.get(url, as_json=False)
            root = ET.fromstring(xml_text)
            ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}

            for entry in root.findall("atom:entry", ns):
                id_node = entry.find("atom:id", ns)
                if id_node is None or not id_node.text:
                    continue
                aid = id_node.text.rsplit("/abs/", 1)[-1].split("v")[0]
                if aid not in papers_dict:
                    continue

                title = entry.findtext("atom:title", "", ns).replace("\n", " ").strip()
                published = entry.findtext("atom:published", "", ns).strip()
                summary = entry.findtext("atom:summary", "", ns)
                comment = entry.findtext("arxiv:comment", "", ns)

                authors = [a.findtext("atom:name", "", ns).strip() for a in entry.findall("atom:author", ns)]

                # Extract GitHub link from comments or abstract if HF didn't have it
                combined_text = f"{summary} {comment}"
                gh_match = clean_github_repo(combined_text)
                if gh_match and not papers_dict[aid].get("github_url"):
                    papers_dict[aid]["github_url"] = f"https://github.com/{gh_match[0]}/{gh_match[1]}"

                if title:
                    papers_dict[aid]["title"] = title
                papers_dict[aid]["authors"] = authors or ["Unknown"]
                papers_dict[aid]["published_date"] = published or datetime.now(timezone.utc).isoformat()

            # Strictly honor arXiv's 1-request-per-3-seconds rate limit
            await asyncio.sleep(3.1)
        except Exception as e:
            log.warning("papers.arxiv_batch_error", batch_offset=i, error=str(e))

async def fetch_github_stars_batched(http: HttpClient, repos: list[tuple[str, str]], tokens: list[str]) -> dict[tuple[str, str], int]:
    """Queries GitHub GraphQL API in batches of 50 repositories per query."""
    if not tokens:
        log.warning("papers.no_github_token", msg="No GITHUB_TOKENS found. Defaulting github_stars to 0.")
        return {}

    stars_map = {}
    token_idx = 0
    repo_list = list(set(repos))
    log.info("papers.github_stars_fetch_start", total_repos=len(repo_list))

    for i in range(0, len(repo_list), 50):
        batch = repo_list[i:i + 50]
        query_parts = []
        for idx, (owner, name) in enumerate(batch):
            alias = f"repo_{idx}"
            query_parts.append(
                f'{alias}: repository(owner: "{owner}", name: "{name}") {{ nameWithOwner stargazerCount }}'
            )

        query = f"query {{ {' '.join(query_parts)} }}"
        headers = {"Authorization": f"bearer {tokens[token_idx % len(tokens)]}"}

        try:
            data = await http.post("https://api.github.com/graphql", json={"query": query}, headers=headers, as_json=True)
            res_data = data.get("data") or {}
            for idx, (owner, name) in enumerate(batch):
                alias = f"repo_{idx}"
                repo_node = res_data.get(alias)
                if repo_node and "stargazerCount" in repo_node:
                    stars_map[(owner, name)] = repo_node["stargazerCount"]
        except Exception as e:
            log.warning("papers.github_graphql_batch_failed", error=str(e))
            token_idx += 1  # rotate to next token on error

    log.info("papers.github_stars_fetch_complete", resolved=len(stars_map))
    return stars_map

async def run_papers_extractor(http: HttpClient, target_count: int = 1000) -> list[ResearchPaperRecord]:
    papers_dict = await fetch_hf_daily_papers(http, days_back=180)
    await enrich_via_arxiv_api(http, papers_dict, limit=target_count + 500)

    # Filter papers with valid GitHub repositories
    papers_with_code = []
    repo_tuples = []
    for aid, p in papers_dict.items():
        gh_tuple = clean_github_repo(p.get("github_url"))
        if gh_tuple:
            papers_with_code.append((p, gh_tuple))
            repo_tuples.append(gh_tuple)

    log.info("papers.correlated_with_code", count=len(papers_with_code))

    stars_map = await fetch_github_stars_batched(http, repo_tuples, settings.github_token_list)

    now_iso = datetime.now(timezone.utc).isoformat()
    records = []

    for p, (owner, repo) in papers_with_code:
        stars = stars_map.get((owner, repo), 0)
        rec = ResearchPaperRecord(
            schemaVersion="1.0",
            recordType="RESEARCH_PAPER",
            source=SourceInfo(
                name="arXiv / Hugging Face Papers",
                url=f"https://arxiv.org/abs/{p['id']}"
            ),
            content=ResearchPaperContent(
                title=p.get("title", f"Paper {p['id']}"),
                authors=p.get("authors", ["AI Research Collective"]),
                paper_url=f"https://arxiv.org/abs/{p['id']}",
                github_url=f"https://github.com/{owner}/{repo}",
                github_stars=stars,
                published_date=p.get("published_date") or now_iso
            ),
            collectedAt=now_iso
        )
        records.append(rec)
        if len(records) >= target_count:
            break

    # Save to JSONL (UTF-8 encoding enforced for Windows)
    out_file = settings.OUTPUT_DIR / "research_papers.jsonl"
    with open(out_file, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r.model_dump(), ensure_ascii=False) + "\n")

    log.info("papers.saved_to_disk", count=len(records), path=str(out_file))
    return records