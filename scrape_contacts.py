#!/usr/bin/env python3
"""
Scrape public contact details (store name, email, phone) from the footer of
each store in list.txt.

Usage:
    pip install -r requirements.txt
    python scrape_contacts.py                       # list.txt -> contacts.csv
    python scrape_contacts.py --limit 50            # quick test on 50 domains
    python scrape_contacts.py --contact-page        # if footer has nothing, try /pages/contact
    python scrape_contacts.py --threads 8 --delay 1.5

Notes:
  * Only reads public pages, respects each site's robots.txt, one request per
    page, and waits between requests.
  * Resumable: domains already in contacts.csv are skipped.
"""
import argparse, csv, os, re, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib import robotparser
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
FIELDS = ["domain", "store_name", "footer_name", "emails", "phones", "whatsapp", "source", "status"]

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
BAD_EMAIL_PARTS = ("sentry", "example.", "wixpress", "shopify.com", "yourdomain", "domain.com",
                   "email.com", "@2x", "@3x", "u003e", "your@", "name@", "user@")
BAD_EMAIL_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js", ".woff", ".woff2")

# Pakistani mobile (03xx xxxxxxx / +92 3xx xxxxxxx) and landline numbers
MOBILE_RE = re.compile(r"(?<!\d)(?:\+?92|0092|0)[\s\-.]?\(?3\d{2}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}(?!\d)")
LANDLINE_RE = re.compile(r"(?<!\d)(?:\+?92|0092|0)[\s\-.]?\(?[1-9]\d{1,2}\)?[\s\-.]?\d{6,8}(?!\d)")
WA_RE = re.compile(r"(?:wa\.me/|whatsapp\.com/send\?[^\"'\s]*phone=)\+?(\d{10,15})", re.I)
COPY_RE = re.compile(
    r"(?:©|&copy;|\(c\)|copyright)\s*(?:\d{4}(?:\s*[-–]\s*\d{4})?)?[\s,.\-–:]*"
    r"([A-Za-z0-9][^\n|©]{1,60}?)(?=\s*(?:\.|,|\||all rights|powered|\n|$))",
    re.I,
)


# ----------------------------------------------------------------- helpers
def decode_cf_email(enc):
    """Decode Cloudflare-obfuscated emails (data-cfemail)."""
    try:
        key = int(enc[:2], 16)
        return "".join(chr(int(enc[i:i + 2], 16) ^ key) for i in range(2, len(enc), 2))
    except ValueError:
        return ""


def clean_email(e):
    e = unquote(e).strip().strip(".,;:()<>[]\"'").lower()
    if not EMAIL_RE.fullmatch(e):
        return ""
    if e.endswith(BAD_EMAIL_EXT) or any(b in e for b in BAD_EMAIL_PARTS):
        return ""
    return e


def normalize_phone(raw):
    d = re.sub(r"\D", "", raw)
    if d.startswith("0092"):
        d = d[2:]
    elif d.startswith("0"):
        d = "92" + d[1:]
    if d.startswith("92") and 11 <= len(d) <= 12:
        return "+" + d
    return ""


def footer_nodes(soup):
    nodes = list(soup.find_all("footer"))
    for el in soup.find_all(["div", "section", "aside"]):
        ident = " ".join(el.get("class", [])) + " " + (el.get("id") or "")
        if "footer" in ident.lower():
            nodes.append(el)
    return nodes


def pick_store_name(soup, domain):
    og = soup.find("meta", attrs={"property": "og:site_name"})
    if og and og.get("content", "").strip():
        return og["content"].strip()
    if soup.title and soup.title.string:
        parts = [p.strip() for p in re.split(r"\s[|\-–—]\s", soup.title.string) if p.strip()]
        parts = [p for p in parts if p.lower() not in ("home", "home page", "homepage")]
        if parts:
            return parts[-1] if len(parts) > 1 and len(parts[0]) > 40 else parts[0]
    return domain.split(".")[0]


def extract_from_nodes(nodes):
    emails, phones, whatsapp, copy_name = set(), set(), set(), ""
    for n in nodes:
        for a in n.find_all("a", href=True):
            href = a["href"].strip()
            low = href.lower()
            if low.startswith("mailto:"):
                e = clean_email(href[7:].split("?")[0])
                if e:
                    emails.add(e)
            elif low.startswith("tel:"):
                p = normalize_phone(unquote(href[4:]))
                if p:
                    phones.add(p)
                else:
                    d = re.sub(r"[^\d+]", "", unquote(href[4:]))
                    if 8 <= len(d.lstrip("+")) <= 15:
                        phones.add(d if d.startswith("+") else "+" + d)
            m = WA_RE.search(href)
            if m:
                whatsapp.add("+" + m.group(1))
        for tag in n.find_all(attrs={"data-cfemail": True}):
            e = clean_email(decode_cf_email(tag["data-cfemail"]))
            if e:
                emails.add(e)
        text = n.get_text("\n", strip=True)
        for e in EMAIL_RE.findall(text):
            e = clean_email(e)
            if e:
                emails.add(e)
        for rx in (MOBILE_RE, LANDLINE_RE):
            for m in rx.findall(text):
                p = normalize_phone(m)
                if p:
                    phones.add(p)
        if not copy_name:
            for m in COPY_RE.finditer(text):
                cand = re.sub(r"\s*all rights reserved.*", "", m.group(1), flags=re.I).strip(" .,-–")
                if cand and "powered by" not in cand.lower() and "shopify" not in cand.lower():
                    copy_name = cand
                    break
    return emails, phones, whatsapp, copy_name


def parse_html(html, domain, whole_page=False):
    soup = BeautifulSoup(html, "html.parser")
    store_name = pick_store_name(soup, domain)
    for t in soup(["script", "style", "noscript", "template"]):
        t.decompose()
    nodes = [soup.body or soup] if whole_page else footer_nodes(soup)
    emails, phones, whatsapp, copy_name = extract_from_nodes(nodes)
    return {
        "store_name": store_name,
        "footer_name": copy_name,
        "emails": ";".join(sorted(emails)),
        "phones": ";".join(sorted(phones)),
        "whatsapp": ";".join(sorted(whatsapp)),
    }


# ----------------------------------------------------------------- network
def robots_allows(base):
    try:
        r = requests.get(base + "/robots.txt", headers=HEADERS, timeout=8)
        if r.status_code >= 400:
            return True
        rp = robotparser.RobotFileParser()
        rp.parse(r.text.splitlines())
        return rp.can_fetch("*", base + "/")
    except requests.RequestException:
        return True


def fetch(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
        if r.ok and "html" in r.headers.get("content-type", "").lower():
            return r.text
    except requests.RequestException:
        pass
    return None


def scrape_domain(domain, contact_page, delay):
    row = {k: "" for k in FIELDS}
    row["domain"] = domain
    base = None
    html = None
    for scheme in ("https", "http"):
        base = f"{scheme}://{domain}"
        if not robots_allows(base):
            row["status"] = "blocked_by_robots"
            return row
        html = fetch(base + "/")
        if html:
            break
    if not html:
        row["status"] = "unreachable"
        return row

    row.update(parse_html(html, domain))
    row["source"] = "footer"

    if contact_page and not row["emails"] and not row["phones"]:
        for path in ("/pages/contact", "/pages/contact-us"):
            time.sleep(delay)
            page = fetch(base + path)
            if page:
                found = parse_html(page, domain, whole_page=True)
                if found["emails"] or found["phones"] or found["whatsapp"]:
                    for k in ("emails", "phones", "whatsapp"):
                        row[k] = found[k]
                    row["source"] = "contact-page"
                    break

    row["status"] = "ok" if (row["emails"] or row["phones"] or row["whatsapp"]) else "no_contacts_found"
    time.sleep(delay)
    return row


# -------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="list.txt")
    ap.add_argument("--output", default="contacts.csv")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--delay", type=float, default=1.0, help="seconds each worker waits after a site")
    ap.add_argument("--limit", type=int, default=0, help="only process the first N new domains")
    ap.add_argument("--contact-page", action="store_true", help="fallback to /pages/contact if footer is empty")
    a = ap.parse_args()

    with open(a.input) as f:
        domains = [l.strip().lower().replace("https://", "").replace("http://", "").strip("/")
                   for l in f if l.strip()]
    domains = list(dict.fromkeys(domains))

    done = set()
    exists = os.path.exists(a.output)
    if exists:
        with open(a.output, newline="") as f:
            done = {r["domain"] for r in csv.DictReader(f)}
    todo = [d for d in domains if d not in done]
    if a.limit:
        todo = todo[: a.limit]
    print(f"{len(domains)} domains, {len(done)} already done, {len(todo)} to scrape")

    with open(a.output, "a", newline="", encoding="utf-8") as out:
        w = csv.DictWriter(out, fieldnames=FIELDS)
        if not exists:
            w.writeheader()
        with ThreadPoolExecutor(max_workers=a.threads) as ex:
            futs = {ex.submit(scrape_domain, d, a.contact_page, a.delay): d for d in todo}
            for i, fut in enumerate(as_completed(futs), 1):
                try:
                    row = fut.result()
                except Exception as e:  # keep the crawl alive
                    row = {k: "" for k in FIELDS}
                    row.update(domain=futs[fut], status=f"error:{type(e).__name__}")
                w.writerow(row)
                out.flush()
                print(f"[{i}/{len(todo)}] {row['domain']}: {row['status']} {row['emails']} {row['phones']}")
    print("Done ->", a.output)


if __name__ == "__main__":
    sys.exit(main())
