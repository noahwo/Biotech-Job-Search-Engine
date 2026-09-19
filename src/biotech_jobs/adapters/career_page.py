from __future__ import annotations
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from .base import BaseAdapter
from biotech_jobs.models import Job
import json, re, hashlib

TITLE_HINT = re.compile(
    r"bioinform|computational|scientist|data science|data scientist|genomic|microbiom|"
    r"metagenom|informatics|algorithm|research|director|manager|engineer|developer|"
    r"software|application|analyst|microbiolog|ferment|bioprocess|food techn|food sci|"
    r"probiotic|protein|laboratory|lab technician|strain|cultivation|brew",
    re.I,
)
EXTERNAL_JOB_HOST_HINTS = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "recruitee.com", "gem.com",
    "oraclecloud.com", "smartrecruiters.com", "myworkdayjobs.com", "jobvite.com",
    "icims.com", "workable.com", "isolvedhire.com", "jibecdn.com", "recright.com",
    "personio.de", "teamtailor.com", "bamboohr.com",
)


def _jsonld_job(soup):
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            obj=json.loads(tag.string or "{}")
            objs=obj if isinstance(obj,list) else [obj]
            for x in objs:
                if isinstance(x,dict) and x.get("@type")=="JobPosting": return x
        except Exception: pass
    return None


def _from_jsonld(company, x, fallback_url, source="career_page"):
    loc=x.get("jobLocation",{})
    if isinstance(loc,list): loc=loc[0] if loc else {}
    addr=loc.get("address",{}) if isinstance(loc,dict) else {}
    location=", ".join(filter(None,[addr.get("addressLocality"),addr.get("addressRegion"),addr.get("addressCountry")]))
    ju=x.get("url") or fallback_url
    ident=x.get("identifier")
    sid=str(ident.get("value") if isinstance(ident,dict) else ident or ju)
    return Job(company=company["name"],title=x.get("title", ""),location=location,url=ju,source=source,source_id=sid,
               description=BeautifulSoup(x.get("description", ""),"html.parser").get_text(" ",strip=True),raw=x)

class CareerPageAdapter(BaseAdapter):
    """Conservative collector for small-company or nonstandard careers pages.

    Prefers JobPosting JSON-LD. For explicit relevant job links it performs bounded
    detail enrichment and uses JSON-LD when available. Static pages can provide
    `seed_titles` for known named openings.
    """
    def fetch(self, company: dict):
        url=company["url"]
        r=self.session.get(url,timeout=self.timeout); r.raise_for_status()
        soup=BeautifulSoup(r.text,"html.parser")
        page_text=soup.get_text(" ",strip=True)
        emitted=set()
        # Any structured jobs embedded on the listing page.
        for tag in soup.find_all("script",type="application/ld+json"):
            try:
                obj=json.loads(tag.string or "{}")
                objs=obj if isinstance(obj,list) else [obj]
                for x in objs:
                    if not isinstance(x,dict) or x.get("@type")!="JobPosting": continue
                    j=_from_jsonld(company,x,url); emitted.add(j.url); yield j
            except Exception: pass
        host=urlparse(url).netloc
        budget=int(company.get("max_detail_requests",30)); used=0
        for a in soup.find_all("a",href=True):
            text=a.get_text(" ",strip=True); href=urljoin(url,a["href"])
            if href in emitted or href.rstrip('/')==url.rstrip('/') or not text or not TITLE_HINT.search(text): continue
            target_host=urlparse(href).netloc
            if target_host!=host and not any(d in target_host or d in href for d in EXTERNAL_JOB_HOST_HINTS): continue
            if not any(k in href.lower() for k in ["/job","/career","/position","/opening","apply"]): continue
            title=text[:180]; location=""; desc=""; raw={}; sid=hashlib.sha1(href.encode()).hexdigest()[:16]
            if used<budget:
                used+=1
                try:
                    rr=self.session.get(href,timeout=self.timeout); rr.raise_for_status(); ss=BeautifulSoup(rr.text,"html.parser")
                    x=_jsonld_job(ss)
                    if x:
                        j=_from_jsonld(company,x,href); emitted.add(j.url); yield j; continue
                    desc=ss.get_text(" ",strip=True)[:12000]
                    h1=ss.find(["h1","h2"])
                    if h1 and TITLE_HINT.search(h1.get_text(" ",strip=True)): title=h1.get_text(" ",strip=True)[:180]
                except Exception: pass
            emitted.add(href)
            yield Job(company=company["name"],title=title,location=location,url=href,source="career_page",source_id=sid,description=desc,raw=raw)

        # Optional explicit parsing for careers pages that render job titles as headings
        # without individual job-detail links (e.g. server-rendered filtered lists).
        if company.get("heading_jobs"):
            marker=None
            marker_re=re.compile(
                r"open positions?|current openings?|career opportunities|job opportunities|"
                r"openings|vacancies|join us|we seek",
                re.I,
            )
            for h in soup.find_all(["h1","h2","h3","h4","h5"]):
                if marker_re.search(h.get_text(" ",strip=True)):
                    marker=h; break
            if marker:
                for h in marker.find_all_next(["h2","h3","h4","h5","h6"]):
                    title=h.get_text(" ",strip=True)
                    if not title or len(title)>180 or title.lower() in {"open positions","view more","nothing found"}:
                        continue
                    # Stop if we clearly leave the jobs section.
                    prev_heads=[x.get_text(" ",strip=True) for x in h.find_all_previous(["h1","h2","h3"],limit=1)]
                    if prev_heads and marker_re.search(prev_heads[0]) is None and len(emitted)>0:
                        # Do not aggressively stop on nested layouts; only stop after jobs have begun
                        # and a clearly unrelated major section is reached.
                        if any(k in prev_heads[0].lower() for k in ["benefits","culture","values","about us","locations"]):
                            break
                    loc=""
                    nxt=h.find_next_sibling()
                    if nxt and nxt.name not in {"h4","h5"}:
                        txt=nxt.get_text(" ",strip=True)
                        if 0 < len(txt) < 120: loc=txt
                    sid=hashlib.sha1((url+title+loc).encode()).hexdigest()[:16]
                    synthetic=url+"#"+re.sub(r"[^a-z0-9]+","-",title.lower()).strip("-")
                    if synthetic in emitted: continue
                    emitted.add(synthetic)
                    yield Job(company=company["name"],title=title,location=loc,url=url,source="career_page",source_id=sid,
                              description=(title+" "+loc+" "+page_text[:8000]).strip(),raw={"heading_job":True})

        for seed in company.get("seed_titles",[]) or []:
            title=seed if isinstance(seed,str) else seed.get("title","")
            if title and title.lower() in page_text.lower():
                sid=hashlib.sha1((url+title).encode()).hexdigest()[:16]
                yield Job(company=company["name"],title=title,location=company.get("default_location",""),url=url,
                          source="career_page",source_id=sid,description=page_text[:12000])
