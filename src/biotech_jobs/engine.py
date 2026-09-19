from __future__ import annotations
import csv
from pathlib import Path
from datetime import datetime, timezone
import signal
import time
import uuid
import yaml
from biotech_jobs.adapters import ADAPTERS
from biotech_jobs.scoring import score_job, alert_location_eligible
from biotech_jobs.store import JobStore
from biotech_jobs.compensation import apply_compensation


class CompanyTimeoutError(TimeoutError):
    pass


def _timeout_handler(signum, frame):
    raise CompanyTimeoutError("company retrieval exceeded hard timeout")


class Engine:
    def __init__(self, config_path, db_path="jobs.sqlite", company_timeout=75, request_timeout=12):
        self.config_path = Path(config_path)
        self.config = yaml.safe_load(self.config_path.read_text())
        self.store = JobStore(db_path)
        self.company_timeout = int(company_timeout)
        self.request_timeout = int(request_timeout)

    @staticmethod
    def _log(message: str):
        print(message, flush=True)

    def _fetch_with_timeout(self, adapter, company, seconds):
        """Fetch one company with a hard wall-clock timeout on Unix/GitHub runners."""
        if seconds <= 0 or not hasattr(signal, "SIGALRM"):
            return list(adapter.fetch(company))
        previous = signal.getsignal(signal.SIGALRM)
        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(seconds)
        try:
            return list(adapter.fetch(company))
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)

    def run(self, min_score=70):
        started = datetime.now(timezone.utc)
        run_id = started.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        results, errors, warnings = [], [], []
        active_target_profile = self.config.get("active_target_profile")
        enabled = [
            c for c in self.config.get("companies", [])
            if c.get("enabled", True)
            and (not active_target_profile or c.get("target_profile") == active_target_profile)
        ]
        attempted = 0
        new_count = 0
        profile_suffix = f" for target profile {active_target_profile!r}" if active_target_profile else ""
        self._log(f"Starting biotech job search — {len(enabled)} enabled companies{profile_suffix}")
        self._log(f"Request timeout: {self.request_timeout}s | company hard timeout: {self.company_timeout}s")

        for idx, company in enumerate(enabled, start=1):
            attempted += 1
            platform = company["platform"]
            name = company["name"]
            adapter_cls = ADAPTERS.get(platform)
            self._log(f"[{idx:02d}/{len(enabled):02d}] {name} ({platform}) ...")
            if company.get("diagnostic"):
                target=company.get("url") or company.get("board") or company.get("host") or ""
                if target:
                    self._log(f"         target: {target}")
            t0 = time.monotonic()
            if not adapter_cls:
                msg = "Unknown platform"
                errors.append({"company": name, "platform": platform, "error": msg})
                self._log(f"         ERROR: {msg} — continuing")
                continue

            adapter = adapter_cls(timeout=int(company.get("request_timeout", self.request_timeout)))
            seen = []
            try:
                company_timeout = int(company.get("company_timeout", self.company_timeout))
                jobs = self._fetch_with_timeout(adapter, company, company_timeout)
                match_count = 0
                company_new = 0
                for job in jobs:
                    apply_compensation(job)
                    score = score_job(job)
                    is_new = self.store.upsert(job, score, now=started.isoformat())
                    new_count += int(is_new)
                    company_new += int(is_new)
                    alert_eligible = alert_location_eligible(job)
                    match_count += int(score.total >= min_score and alert_eligible)
                    seen.append(job.stable_key)
                    row = job.to_dict()
                    row.update(score.to_dict())
                    row.update({"alert_eligible": alert_eligible, "is_new": is_new, "run_id": run_id,
                                "first_seen_this_run": started.isoformat() if is_new else ""})
                    results.append(row)
                # A zero result can mean either a genuinely empty board or a collector that
                # failed silently because the careers site is client-rendered. Keep those states distinct.
                adapter_stats_pre = getattr(adapter, "last_stats", {}) or {}
                adapter_verified_empty = bool(adapter_stats_pre.get("verified_empty", False))
                if len(jobs) == 0 and not (company.get("zero_jobs_verified", False) or company.get("zero_result_verified", False) or adapter_verified_empty):
                    warning = {"company": name, "platform": platform, "warning": "ZERO_RESULT_UNVERIFIED"}
                    warnings.append(warning)
                    self._log("         WARNING: ZERO_RESULT_UNVERIFIED — careers infrastructure needs validation")
                elif len(jobs) == 0 and adapter_verified_empty and company.get("diagnostic"):
                    self._log("         Verified empty board from rendered ATS state")
                self.store.mark_missing_inactive(name, seen)
                elapsed = time.monotonic() - t0
                stats = getattr(adapter, "last_stats", {}) or {}
                extra = ""
                if stats:
                    extra = f" | pages {stats.get('pages','?')} | enriched {stats.get('details_enriched','?')}"
                    if "links_discovered" in stats: extra += f" | links {stats.get('links_discovered')}"
                    if "frames" in stats: extra += f" | frames {stats.get('frames')}"
                self._log(
                    f"         Retrieved {len(jobs)} jobs in {elapsed:.1f}s | "
                    f"{match_count} scored >={min_score} | {company_new} first-seen{extra}"
                )
                if company.get("diagnostic") and jobs:
                    titles=[]
                    for j in jobs:
                        t=(getattr(j,"title","") or "").strip()
                        if t and t not in titles:
                            titles.append(t)
                        if len(titles) >= 5:
                            break
                    if titles:
                        self._log("         sample titles: " + " | ".join(titles))
                if company.get("diagnostic") and stats.get("external_links"):
                    self._log("         external: " + str(stats.get("external_links")))
                if company.get("diagnostic") and stats.get("rendered_text_excerpt") and len(jobs)==0:
                    self._log("         rendered: " + str(stats.get("rendered_text_excerpt")))
            except CompanyTimeoutError:
                elapsed = time.monotonic() - t0
                msg = f"timed out after {elapsed:.1f}s"
                errors.append({"company": name, "platform": platform, "error": msg})
                self._log(f"         TIMEOUT: {msg} — continuing")
            except Exception as e:
                elapsed = time.monotonic() - t0
                msg = f"{type(e).__name__}: {e}"
                errors.append({"company": name, "platform": platform, "error": msg})
                self._log(f"         ERROR after {elapsed:.1f}s: {msg} — continuing")

        completed = datetime.now(timezone.utc)
        self.store.record_run(run_id, started.isoformat(), completed.isoformat(), attempted, len(results), new_count, errors)
        elapsed_total = (completed - started).total_seconds()
        self._log(
            f"Completed run in {elapsed_total:.1f}s — {len(results)} jobs retrieved, "
            f"{new_count} first-seen, {len(errors)} company errors"
        )
        return results, errors, warnings, {"run_id": run_id, "started_at": started.isoformat(), "completed_at": completed.isoformat(),
                                 "companies_attempted": attempted, "jobs_retrieved": len(results), "new_jobs": new_count,
                                 "elapsed_seconds": elapsed_total, "warnings": warnings}

    @staticmethod
    def _dedupe_alert_rows(rows):
        """Collapse same company/title alert duplicates while retaining the highest-scoring row.

        Raw postings remain distinct in SQLite; this affects only human-facing reports.
        """
        best = {}
        for r in rows:
            company = " ".join(str(r.get("company", "")).lower().split())
            title = " ".join(str(r.get("title", "")).lower().split())
            key = (company, title)
            cur = best.get(key)
            if cur is None or r.get("total", 0) > cur.get("total", 0):
                best[key] = r
        return list(best.values())

    @staticmethod
    def export_csv(rows, path, min_score=0, new_only=False):
        rows = [r for r in rows if r.get("total", 0) >= min_score
                and r.get("alert_eligible", True)
                and (not new_only or r.get("is_new"))]
        rows = Engine._dedupe_alert_rows(rows)
        rows.sort(key=lambda r: (r.get("total", 0), r.get("company", ""), r.get("title", "")), reverse=True)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        if not rows:
            Path(path).write_text("")
            return 0
        fields = list(rows[0].keys())
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader(); w.writerows(rows)
        return len(rows)
