from __future__ import annotations
from urllib.parse import urljoin, urlparse
import hashlib, re
from bs4 import BeautifulSoup
from .base import BaseAdapter
from biotech_jobs.models import Job

TITLE_HINT = re.compile(
    r"bioinform|computational|scientist|data science|data scientist|genomic|microbiom|"
    r"metagenom|informatics|algorithm|research|director|manager|engineer|application|"
    r"developer|analyst|microbiolog|ferment|bioprocess|food techn|food sci|probiotic|"
    r"protein|laboratory|lab technician|strain|cultivation|brew",
    re.I,
)
JOB_HREF = re.compile(r"(/job/|/jobs/|/position/|/positions/|/opening/|/openings/|jobid=|gh_jid=|jobId=|requisition|careersection)", re.I)
DEFAULT_ENTRY_TEXT = r"(search jobs|view all jobs|current opportunities|current openings|explore job opportunities|explore open positions|view openings|job openings|open roles|see current opportunities|view our open roles|find job openings|view and apply to open positions|career center)"
ENTRY_TEXT = re.compile(DEFAULT_ENTRY_TEXT, re.I)
GENERIC_LINK_LABELS = {
    "applications", "applicant tracking system by teamtailor", "apply here", "apply here →",
    "careers", "embedded job board", "employee login", "explore all open positions",
    "job opening", "job openings", "log in as employee", "open positions", "open roles",
    "read more and apply", "social menu", "skip to main content",
    "submit your open application (opens in a new window)",
}
ATS_HOSTS = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "gem.com", "myworkdayjobs.com",
    "oraclecloud.com", "smartrecruiters.com", "successfactors.com", "phenompeople.com",
    "icims.com", "jibecdn.com", "workforcenow.adp.com", "jobvite.com", "breezy.hr",
    "recright.com", "personio.de", "teamtailor.com", "bamboohr.com",
)


def _likely_job_link(href: str, text: str, root_host: str) -> bool:
    if not href or href.startswith(("mailto:","tel:","javascript:")):
        return False
    host=urlparse(href).netloc
    return bool(JOB_HREF.search(href) or any(x in host for x in ATS_HOSTS) or (host==root_host and TITLE_HINT.search(text or "")))


class BrowserCareerPageAdapter(BaseAdapter):
    """Browser-rendered fallback for JS-heavy careers sites.

    Collects visible job links from the main document and child frames. It only performs
    expensive detail enrichment for titles that look scientifically relevant, but it
    records all discovered job links so a healthy board cannot appear as a false zero.
    """
    def fetch(self, company: dict):
        try:
            from playwright.sync_api import sync_playwright
        except Exception as e:
            raise RuntimeError("browser_career requires playwright; install the browser extra") from e

        url=company["url"]
        max_detail=int(company.get("max_detail_requests",30))
        root_host=urlparse(url).netloc
        discovered=[]; seen=set(); frames_seen=0
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True)
            page=browser.new_page()
            browser_error=None
            try:
                page.goto(url,wait_until="domcontentloaded",timeout=max(30000,self.timeout*1000))
                page.wait_for_timeout(int(company.get("render_wait_ms",3000)))
            except Exception as e:
                browser_error=e
            # Scroll a few times for lazy-loaded boards.
            if browser_error is None:
                for _ in range(int(company.get("scroll_passes",4))):
                    try:
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        page.wait_for_timeout(800)
                    except Exception:
                        pass
            entry_links=[]
            def collect_from_frames(frames, base_url):
                nonlocal frames_seen
                for frame in frames:
                    frames_seen += 1
                    try:
                        anchors=frame.locator("a").all()
                    except Exception:
                        continue
                    for a in anchors:
                        try:
                            href=a.get_attribute("href") or ""
                            label=(a.inner_text() or "").strip()
                        except Exception:
                            continue
                        full=urljoin(frame.url or base_url,href)
                        if not href:
                            continue
                        normalized_label=" ".join(label.lower().split())
                        if normalized_label in GENERIC_LINK_LABELS:
                            continue
                        parsed_path=urlparse(full).path.rstrip("/")
                        if not normalized_label and not JOB_HREF.search(full) and parsed_path in {"", "/careers"}:
                            continue
                        low=(label+" "+full).lower()
                        custom_terms=company.get("entry_text_terms") or []
                        entry_match=ENTRY_TEXT.search(label or "") or any(str(t).lower() in (label or "").lower() for t in custom_terms)
                        if entry_match and full not in seen:
                            entry_links.append(full)
                        if full in seen or not _likely_job_link(full,label,root_host):
                            continue
                        if any(x in low for x in ["privacy","terms","talent community","join our talent","career home"]):
                            continue
                        seen.add(full); discovered.append((label[:180] or "Job opening",full,"",""))
            if browser_error is None:
                collect_from_frames(page.frames,url)
                if company.get("heading_jobs"):
                    heading_terms = re.compile(
                        r"scientist|research|microbiolog|ferment|bioprocess|protein|laboratory|"
                        r"technician|operator|engineer|food|strain|cultivation|brew",
                        re.I,
                    )
                    for h in page.locator("h2, h3, h4, h5, h6").all():
                        try:
                            label=(h.inner_text() or "").strip()
                        except Exception:
                            continue
                        if not label or len(label) > 180 or not heading_terms.search(label):
                            continue
                        if " ".join(label.lower().split()) in GENERIC_LINK_LABELS:
                            continue
                        synthetic=url+"#"+re.sub(r"[^a-z0-9]+","-",label.lower()).strip("-")
                        if synthetic in seen:
                            continue
                        seen.add(synthetic); discovered.append((label[:180],synthetic,"",""))
                rendered_selector=company.get("rendered_job_selector")
                if rendered_selector:
                    title_selector=company.get("rendered_job_title_selector")
                    link_selector=company.get("rendered_job_link_selector", "a[href]")
                    description_selector=company.get("rendered_job_description_selector")
                    location_attribute=company.get("rendered_job_location_attribute")
                    for card in page.locator(rendered_selector).all():
                        try:
                            title_node=card.locator(title_selector).first if title_selector else card
                            label=" ".join((title_node.inner_text() or "").split())
                            link_node=card.locator(link_selector).first
                            href=link_node.get_attribute("href") or ""
                            full=urljoin(page.url,href)
                            desc=""
                            if description_selector and card.locator(description_selector).count():
                                desc=" ".join((card.locator(description_selector).first.inner_text() or "").split())[:16000]
                            loc=(card.get_attribute(location_attribute) or "").strip() if location_attribute else ""
                        except Exception:
                            continue
                        if not label or not href or full in seen:
                            continue
                        seen.add(full); discovered.append((label[:180],full,loc[:120],desc))
                if company.get("browser_pagination"):
                    maxp=int(company.get("max_browser_pages",4))
                    for _page_no in range(2,maxp+1):
                        try:
                            nxt=page.locator("a",has_text=re.compile(r"^(next|>|›|»)$",re.I))
                            if nxt.count()==0: break
                            href=nxt.first.get_attribute("href")
                            if href:
                                page.goto(urljoin(page.url,href),wait_until="domcontentloaded",timeout=max(30000,self.timeout*1000))
                            else:
                                nxt.first.click()
                                page.wait_for_timeout(1500)
                            collect_from_frames(page.frames,page.url)
                        except Exception:
                            break
                if company.get("follow_entry_links") and not discovered:
                    for entry in list(dict.fromkeys(entry_links))[:3]:
                        try:
                            page.goto(entry,wait_until="domcontentloaded",timeout=max(30000,self.timeout*1000))
                            page.wait_for_timeout(int(company.get("render_wait_ms",3000)))
                            collect_from_frames(page.frames,entry)
                        except Exception:
                            continue
            browser.close()

        # Some corporate sites fail Chromium with HTTP/2 errors while ordinary HTTPS remains readable.
        # Fall back to server HTML rather than failing the company outright.
        if not discovered:
            try:
                rr=self.session.get(url,timeout=self.timeout,allow_redirects=True); rr.raise_for_status()
                ss=BeautifulSoup(rr.text,"html.parser")
                for node in ss.find_all(["a","iframe"]):
                    href=node.get("href") or node.get("src") or ""
                    label=node.get_text(" ",strip=True) if node.name=="a" else "Embedded job board"
                    full=urljoin(url,href)
                    if full in seen or not href:
                        continue
                    if " ".join(label.lower().split()) in GENERIC_LINK_LABELS:
                        continue
                    if _likely_job_link(full,label,root_host) or any(x in urlparse(full).netloc for x in ATS_HOSTS):
                        seen.add(full); discovered.append((label[:180] or "Job opening",full,"",""))
            except Exception:
                if browser_error is not None:
                    raise browser_error

        self.last_stats={"pages":1,"details_enriched":0,"frames":frames_seen,"links_discovered":len(discovered)}
        if company.get("log_external_links"):
            try:
                ext=[]
                rr=self.session.get(url,timeout=self.timeout,allow_redirects=True)
                if rr.ok:
                    ss=BeautifulSoup(rr.text,"html.parser")
                    root=urlparse(url).netloc
                    for a in ss.find_all("a",href=True):
                        full=urljoin(url,a["href"])
                        host=urlparse(full).netloc
                        label=a.get_text(" ",strip=True)
                        if host and host != root and label and len(label)<160:
                            ext.append(f"{label} -> {full}")
                self.last_stats["external_links"]=" || ".join(ext[:8])
            except Exception:
                pass
        used=0
        for label,href,location,desc in discovered:
            title=label; raw={"listing_url":url}
            # Enrich likely relevant jobs. Non-relevant postings are still retained for board-health tracking.
            if not desc and (company.get("enrich_all") or TITLE_HINT.search(title or "")) and used < max_detail:
                used += 1
                try:
                    rr=self.session.get(href,timeout=self.timeout,allow_redirects=True); rr.raise_for_status()
                    soup=BeautifulSoup(rr.text,"html.parser")
                    h1=soup.find(["h1","h2"])
                    if h1 and len(h1.get_text(" ",strip=True)) < 200:
                        title=h1.get_text(" ",strip=True)
                    desc=soup.get_text(" ",strip=True)[:16000]
                    # Common location hints.
                    m=re.search(r"(?:Location|Locations?)\s*[:\-]?\s*([A-Za-z][A-Za-z .,';/\-]{2,100})",desc,re.I)
                    if m: location=m.group(1).strip()[:120]
                except Exception:
                    pass
            sid=hashlib.sha1(href.encode()).hexdigest()[:20]
            if " ".join(title.lower().split()) in GENERIC_LINK_LABELS:
                continue
            yield Job(company=company["name"],title=title,location=location,url=href,source="browser_career",source_id=sid,description=desc,raw=raw)
        self.last_stats["details_enriched"]=used
