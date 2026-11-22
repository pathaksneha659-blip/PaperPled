import csv
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import json
import tldextract
from rapidfuzz.fuzz import WRatio
from unidecode import unidecode


LEGAL_SUFFIXES = (
    "incorporated", "inc", "llc", "limited", "ltd", "corp", "corporation",
    "pbc", "gmbh", "sas", "labs", "lab", "technologies", "technology",
)


@dataclass(frozen=True)
class Match:
    raw: str
    normalized: str
    compact: str
    canonical: str | None
    method: str
    score: float
    entity_id: str
    source_url: str


class Resolver:
    def __init__(self, seed_path):
        seed_path = Path(seed_path)
        with seed_path.open("r", encoding="utf-8") as handle:
            self.entities = json.load(handle)
        self.log = []
        self.dynamic = {}

        self._domain_index = {}
        self._compact_index = {}
        for entity in self.entities:
            for domain in entity.get("domains", []):
                self._domain_index[domain.lower().strip(".")] = entity
            names = [entity["canonical"], *entity.get("aliases", [])]
            for name in names:
                self._compact_index.setdefault(self.compact(name), []).append(entity)

    @staticmethod
    def normalized(raw: str) -> str:
        return unidecode(raw.strip()).lower()

    @classmethod
    def compact(cls, raw: str) -> str:
        text = unidecode(raw.strip()).lower()
        text = re.sub(r"\bc(?:orporation|orp)\b", "", text)
        text = re.sub(r"[,.'\-_/&+()]", " ", text)
        words = text.split()
        while words and words[-1] in LEGAL_SUFFIXES:
            words.pop()
        return "".join(words)

    @staticmethod
    def _registrable_domain(value: str) -> str | None:
        if not value:
            return None
        text = value.strip()
        if "://" not in text:
            text = "https://" + text
        extracted = tldextract.extract(text)
        if not extracted.domain or not extracted.suffix:
            return None
        return f"{extracted.domain}.{extracted.suffix}".lower()

    def resolve(self, raw: str, domain: str | None = None, source_url: str = "") -> Match:
        raw = raw or ""
        normalized = self.normalized(raw)
        compact = self.compact(raw)
        canonical = None
        method = "new_entity"
        score = 0.0
        entity_id = ""

        if not raw.strip():
            match = Match(raw, normalized, compact, None, "new_entity", 0.0, "", source_url)
            self.log.append(match)
            return match

        candidate_domain = self._registrable_domain(domain)
        if candidate_domain and candidate_domain in self._domain_index:
            entity = self._domain_index[candidate_domain]
            canonical = entity["canonical"]
            method = "registrable_domain"
            score = 100.0
            entity_id = entity["id"]
        else:
            exact = self._compact_index.get(compact)
            if exact:
                entity = exact[0]
                canonical = entity["canonical"]
                method = "exact_compact"
                score = 100.0
                entity_id = entity["id"]

        if canonical is None:
            candidates = []
            seen_ids = set()
            for entity in self.entities:
                names = [entity["canonical"], *entity.get("aliases", [])]
                entity_score = max((WRatio(compact, self.compact(name)) for name in names), default=0.0)
                if entity["id"] not in seen_ids:
                    candidates.append((entity_score, entity))
                    seen_ids.add(entity["id"])

            candidates.sort(key=lambda item: item[0], reverse=True)
            best_score, best_entity = candidates[0] if candidates else (0.0, None)
            second_score = candidates[1][0] if len(candidates) > 1 else 0.0
            threshold = 95 if len(compact) <= 5 else 90

            best_compact = self.compact(best_entity["canonical"]) if best_entity else ""
            length_ratio = (
                min(len(compact), len(best_compact)) / max(len(compact), len(best_compact))
                if compact and best_compact else 0.0
            )

            if (
                best_entity
                and best_score >= threshold
                and best_score - second_score >= 3
                and length_ratio >= 0.72
            ):
                canonical = best_entity["canonical"]
                method = "fuzzy"
                score = best_score
                entity_id = best_entity["id"]
            elif best_score >= 80:
                canonical = None
                method = "ambiguous_review"
                score = best_score
                entity_id = ""
            else:
                entity_id = self.dynamic.setdefault(compact, f"dyn:{compact}")
                canonical = raw.strip()
                method = "new_entity"
                score = best_score

        match = Match(raw, normalized, compact, canonical, method, score, entity_id, source_url)
        self.log.append(match)
        return match

    def export_log(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        columns = ["raw", "normalized", "compact", "canonical", "method", "score", "entity_id", "source_url"]
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(asdict(match) for match in self.log)