#!/usr/bin/env python3
"""
Builds index.html for Oxford Edge's Daily Signal from live RSS feeds.

Adapted from Manoj's Daily Briefing (see agilisys-build-methodology.md in
the Babyzone project folder for the wider pattern family) - two lenses,
general higher education sector news and philanthropy/funder-focused news,
for Oxford Edge (the Centre for Entrepreneurship and Innovation at Christ
Church, Oxford).

No LLM involvement: every headline/summary/link comes verbatim from the
feed's own <title>/<description>/<link>, filtered by the feed's own
published/updated timestamp. This avoids the hallucinated-URL failure mode
of asking an LLM to "search the web" and report back.
"""

import html
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import feedparser
import requests
from bs4 import BeautifulSoup
from googlenewsdecoder import gnewsdecoder

LOOKBACK_HOURS = 24
MAX_ITEMS_PER_SECTION = 4
MIN_ITEMS_PER_SECTION = 2
SCRAPE_TIMEOUT_SECONDS = 6
SCRAPE_MIN_CHARS = 80

# Same root URL always serves today's edition - this must never change,
# since it's what's saved to a phone home screen. Past editions live at
# fixed sub-paths under archive/ instead of replacing it.
SITE_BASE_URL = "https://williamgitty.github.io/oxford-edge-daily-signal/"
ARCHIVE_DIR = "archive"
ARCHIVE_RETENTION_DAYS = 14
ARCHIVE_FILENAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.html$")
PROMO_KEYWORDS = [
    "giveaway",
    "sweepstakes",
    "coupon",
    "promo code",
    "discount code",
    "enter to win",
    "win a ",
    "/deals/",
    # Found live: Google News site: searches on Chronicle of Higher
    # Education / Chronicle of Philanthropy surface job-board listing pages
    # mixed in with real articles - these outlets run their own jobs
    # subdomain and index individual postings the same way as news pages.
    "jobs.chronicle.com",
    "jobs.philanthropy.com",
    "jobs.insidehighered.com",
    "jobs.timeshighereducation.com",
]
USER_AGENT = (
    "Mozilla/5.0 (compatible; OxfordEdgeDailySignalBot/1.0; "
    "+https://github.com/WilliamGitty/oxford-edge-daily-signal)"
)

# Higher education sector news - general context, not funding-specific.
BBC_EDUCATION = "http://feeds.bbci.co.uk/news/education/rss.xml"
GUARDIAN_EDUCATION = "https://www.theguardian.com/education/rss"
INSIDE_HIGHER_ED = "https://www.insidehighered.com/rss.xml"
# Times Higher Education and Chronicle of Higher Education don't expose a
# working direct RSS feed (neither returned a real feed from their own
# site) - Google News's site: operator reaches their content anyway,
# verified live before adding.
GOOGLE_NEWS_THE = "https://news.google.com/rss/search?q=site:timeshighereducation.com&hl=en-GB&gl=GB&ceid=GB:en"
GOOGLE_NEWS_CHRONICLE_HE = "https://news.google.com/rss/search?q=site:chronicle.com&hl=en-GB&gl=GB&ceid=GB:en"

# Philanthropy/funder-focused sources - Oxford Edge's core interest.
# Third Sector, Civil Society News, Chronicle of Philanthropy, and Research
# Professional News all either 403 or 404 on their own guessed feed URLs -
# Google News site: search reaches all four, verified live before adding.
GOOGLE_NEWS_THIRD_SECTOR = "https://news.google.com/rss/search?q=site:thirdsector.co.uk&hl=en-GB&gl=GB&ceid=GB:en"
GOOGLE_NEWS_CIVIL_SOCIETY = "https://news.google.com/rss/search?q=site:civilsociety.co.uk&hl=en-GB&gl=GB&ceid=GB:en"
GOOGLE_NEWS_CHRONICLE_PHILANTHROPY = "https://news.google.com/rss/search?q=site:philanthropy.com&hl=en-GB&gl=GB&ceid=GB:en"
GOOGLE_NEWS_RESEARCH_PROFESSIONAL = "https://news.google.com/rss/search?q=site:researchprofessionalnews.com&hl=en-GB&gl=GB&ceid=GB:en"

# Both lenses requested: general HE sector context, and the philanthropy/
# funder-focused sources that are Oxford Edge's actual core interest.
SECTIONS = [
    {
        "key": "higher_education",
        "title": "Higher Education News",
        "feeds": [BBC_EDUCATION, GUARDIAN_EDUCATION, INSIDE_HIGHER_ED, GOOGLE_NEWS_THE, GOOGLE_NEWS_CHRONICLE_HE],
    },
    {
        "key": "philanthropy_funders",
        "title": "Philanthropy & Funder News",
        "feeds": [
            GOOGLE_NEWS_THIRD_SECTOR,
            GOOGLE_NEWS_CIVIL_SOCIETY,
            GOOGLE_NEWS_CHRONICLE_PHILANTHROPY,
            GOOGLE_NEWS_RESEARCH_PROFESSIONAL,
        ],
    },
]

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def clean_text(raw):
    if not raw:
        return ""
    text = html.unescape(TAG_RE.sub("", raw))
    text = WS_RE.sub(" ", text).strip()
    return text


MAX_SUMMARY_CHARS = 500


def trim_summary(text):
    if not text:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", text)
    out = []
    length = 0
    for part in parts:
        if out and length + len(part) > MAX_SUMMARY_CHARS:
            break
        out.append(part)
        length += len(part) + 1
    return " ".join(out).strip()


def scrape_article_paragraph(url):
    """Fetch the linked article and pull its opening real body text.

    Returns None on any failure so callers can fall back to the feed's own
    teaser summary. Never fabricates text - only extracts what's actually
    on the page.
    """
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=SCRAPE_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "aside", "footer", "header", "figure"]):
            tag.decompose()
        container = soup.find("article") or soup.body
        if container is None:
            return None
        paragraphs = [p.get_text(" ", strip=True) for p in container.find_all("p")]
        paragraphs = [p for p in paragraphs if len(p) > 40]
        paragraphs = list(dict.fromkeys(paragraphs))
        if not paragraphs:
            return None
        return trim_summary(" ".join(paragraphs))
    except requests.RequestException:
        return None


def resolve_real_url(link):
    """Google News RSS links point at a news.google.com redirect, not the
    real article - and it isn't a plain HTTP redirect a normal request can
    follow: it lands on Google's own cookie-consent page instead, which is
    what both a share recipient and this project's own article-scraping
    step would actually see. Decode to the real publisher URL so the
    visible link, the share button, and the scrape step all point
    somewhere real. Falls back to the original link on any failure - a raw
    Google News link that still works via a real browser beats no link at
    all. Only called on the small number of items actually picked for
    display, not every fetched candidate - a real HTTP round-trip per call,
    so it's not worth paying for items that get discarded anyway."""
    if not link.startswith("https://news.google.com/"):
        return link
    try:
        result = gnewsdecoder(link, interval=1)
        if result.get("status") and result.get("decoded_url"):
            return result["decoded_url"]
    except Exception:
        pass
    return link


def is_live_blog(link):
    return "/live/" in link.lower()


def entry_published(entry):
    for attr in ("published_parsed", "updated_parsed"):
        val = getattr(entry, attr, None)
        if val:
            return datetime(*val[:6], tzinfo=timezone.utc)
    return None


def source_name(entry, feed_url):
    # Google News search results are all links through news.google.com (by
    # design - most of this project's sources have no working direct RSS
    # feed, so they're reached via Google News's site: operator instead),
    # so deriving the name from the link's own host would show
    # "news.google.com" for every single item regardless of who actually
    # published it. Google News RSS entries carry the real publisher
    # separately in <source>, so prefer that when it's present.
    source_field = entry.get("source")
    if source_field:
        title = source_field.get("title") if hasattr(source_field, "get") else None
        if title:
            return title

    host = urlparse(entry.get("link", feed_url)).netloc
    host = host.replace("www.", "")
    known = {
        "bbc.co.uk": "BBC News",
        "feeds.bbci.co.uk": "BBC News",
        "theguardian.com": "The Guardian",
        "insidehighered.com": "Inside Higher Ed",
        "timeshighereducation.com": "Times Higher Education",
        "chronicle.com": "The Chronicle of Higher Education",
        "thirdsector.co.uk": "Third Sector",
        "civilsociety.co.uk": "Civil Society News",
        "philanthropy.com": "The Chronicle of Philanthropy",
        "researchprofessionalnews.com": "Research Professional News",
    }
    for k, v in known.items():
        if k in host:
            return v
    return host or "Source"


# Sources that meter or fully paywall articles - checked mechanically
# against the source name, never guessed per-article. These are on the
# requested source list regardless, so this flags rather than excludes them.
PAYWALLED_SOURCES = {
    "Times Higher Education",
    "The Chronicle of Higher Education",
    "The Chronicle of Philanthropy",
}

# Per Lizzie's philanthropy/HE-funding brief (Sept 2026): 10 content tags plus
# a cross-cutting institution tag, multi-tagged per story. Keyword-matched
# against title+summary, not LLM-classified - this project deliberately has
# no AI involvement (see module docstring), so tagging is a heuristic, not a
# judgment call. It will miss stories that don't use these exact terms and
# will occasionally mistag on a loose keyword match - acceptable for a
# filtering aid, not presented as authoritative categorisation.
TAG_ORDER = [
    "hni_gifts",
    "foundations",
    "corporate_philanthropy",
    "government_grants",
    "endowment_management",
    "major_gift_announcements",
    "fundraising_campaigns",
    "sector_trends",
    "regulatory_tax",
    "reputational_governance",
]
TAG_LABELS = {
    "hni_gifts": "HNI gifts",
    "foundations": "Foundations",
    "corporate_philanthropy": "Corporate philanthropy",
    "government_grants": "Government grants",
    "endowment_management": "Endowment management",
    "major_gift_announcements": "Major gift",
    "fundraising_campaigns": "Fundraising campaign",
    "sector_trends": "Sector trends",
    "regulatory_tax": "Regulatory/tax",
    "reputational_governance": "Reputational/governance",
}
TAG_KEYWORDS = {
    "hni_gifts": ["philanthropist", "alumnus", "alumna", "donates $", "donates £", "gift from", "pledges $", "pledges £", "gives $", "gives £", "billionaire donor", "private donor"],
    "foundations": ["foundation", "wellcome trust", "leverhulme", "gates foundation", "ford foundation", "charitable trust", "grant-making"],
    "corporate_philanthropy": ["corporate partnership", "company-funded", "sponsors a chair", "sponsored chair", "csr", "corporate social responsibility", "industry partnership", "funded chair"],
    "government_grants": ["research council", "ukri", "government funding", "public funding", "government grant", "research grant", "innovate uk", "dsit", "spending review"],
    "endowment_management": ["endowment", "investment strategy", "spending policy", "fund performance", "asset allocation"],
    "major_gift_announcements": ["million gift", "billion gift", "largest ever gift", "naming gift", "record donation", "announces gift of", "landmark gift", "transformative gift"],
    "fundraising_campaigns": ["capital campaign", "fundraising campaign", "campaign launch", "campaign target", "campaign raises", "fundraising drive"],
    "sector_trends": ["giving trends", "donor behaviour", "donor behavior", "philanthropy report", "giving report", "sector analysis", "giving survey"],
    "regulatory_tax": ["charity law", "charity commission", "tax relief", "gift aid", "endowment tax", "regulatory change", "tax treatment", "charities act"],
    "reputational_governance": ["controversy", "resigns amid", "naming dispute", "conflict of interest", "donor backlash", "returns donation", "governance row", "steps down amid"],
}
# Peer/benchmark institutions the brief names or implies ("Oxford colleges,
# Cambridge, Harvard, Yale, and similar") - a fixed list, not open-ended
# named-entity extraction, since there's no LLM in this pipeline to do that
# reliably. Extend this list if Lizzie flags a peer institution it's missing.
INSTITUTIONS = [
    "Oxford", "Cambridge", "Harvard", "Yale", "Stanford", "Princeton", "MIT",
    "Imperial College", "UCL", "LSE", "Columbia", "Cornell", "Penn", "Chicago",
    "Duke", "NYU", "Edinburgh", "Manchester",
]

# Cadence, per the brief: major-gift/governance news needs a same-day read;
# foundation/government-grant/regulatory news is fine as a rolling weekly
# view. Implemented as a wider fetch window for those specific tags only -
# still one daily build (see design discussion), not a separate weekly job.
WEEKLY_CADENCE_TAGS = {"foundations", "government_grants", "regulatory_tax"}
URGENT_CADENCE_TAGS = {"hni_gifts", "major_gift_announcements", "reputational_governance"}
WEEKLY_LOOKBACK_HOURS = 24 * 7


def tag_item(title, summary):
    haystack = f"{title} {summary}".lower()
    tags = {tag_id for tag_id, keywords in TAG_KEYWORDS.items() if any(kw in haystack for kw in keywords)}
    institutions = {name for name in INSTITUTIONS if name.lower() in haystack}
    return tags, institutions


def freshness_hours_for_tags(tags):
    if tags & URGENT_CADENCE_TAGS:
        return LOOKBACK_HOURS
    if tags & WEEKLY_CADENCE_TAGS:
        return WEEKLY_LOOKBACK_HOURS
    return LOOKBACK_HOURS


def fetch_recent_items(feed_url, now, keyword=None):
    parsed = feedparser.parse(feed_url)
    items = []
    seen_links = set()
    max_cutoff = now - timedelta(hours=WEEKLY_LOOKBACK_HOURS)
    for entry in parsed.entries:
        published = entry_published(entry)
        if published is None or published < max_cutoff:
            continue
        title = clean_text(entry.get("title", ""))
        summary = trim_summary(clean_text(entry.get("summary", entry.get("description", ""))))
        link = entry.get("link", "")
        if not link.lower().startswith(("http://", "https://")):
            continue
        if link in seen_links:
            continue
        seen_links.add(link)
        promo_haystack = f"{title} {link}".lower()
        if any(kw in promo_haystack for kw in PROMO_KEYWORDS):
            continue
        if keyword:
            haystack = f"{title} {summary}".lower()
            if keyword.lower() not in haystack:
                continue
        tags, institutions = tag_item(title, summary)
        if published < now - timedelta(hours=freshness_hours_for_tags(tags)):
            continue
        source = source_name(entry, feed_url)
        items.append(
            {
                "title": title,
                "summary": summary,
                "link": link,
                "published": published,
                "source": source,
                "paywalled": source in PAYWALLED_SOURCES,
                "tags": tags,
                "institutions": institutions,
            }
        )
    items.sort(key=lambda i: i["published"], reverse=True)
    return items


def select_diverse(candidates):
    """Pick up to MAX_ITEMS_PER_SECTION items, round-robin across sources.

    Candidates must already be sorted by recency (most recent first). This
    stops any single prolific outlet from filling a whole section just
    because it happened to publish the most today - each source gets one
    slot per round before any source gets a second. Rolling live-blogs are
    capped at one slot total, since their timestamp keeps refreshing all
    day. Falls back to a single source's own stories if nothing else in
    the lookback window qualifies - no padding with stale content either way.
    """
    queues = {}
    order = []
    for item in candidates:
        if item["source"] not in queues:
            queues[item["source"]] = []
            order.append(item["source"])
        queues[item["source"]].append(item)

    picked = []
    live_blog_count = 0
    made_progress = True
    while len(picked) < MAX_ITEMS_PER_SECTION and made_progress:
        made_progress = False
        for source in order:
            if len(picked) >= MAX_ITEMS_PER_SECTION:
                break
            queue = queues[source]
            while queue:
                candidate = queue.pop(0)
                if is_live_blog(candidate["link"]):
                    if live_blog_count >= 1:
                        continue
                    live_blog_count += 1
                picked.append(candidate)
                made_progress = True
                break
    return picked


def build_sections():
    now = datetime.now(timezone.utc)
    used_links = set()
    rendered_sections = []

    for section in SECTIONS:
        candidates = []
        for feed_url in section["feeds"]:
            candidates.extend(fetch_recent_items(feed_url, now, keyword=section.get("keyword")))

        # Merge multiple sources by recency, not by feed order.
        candidates.sort(key=lambda i: i["published"], reverse=True)

        # Dedupe: skip stories already claimed by an earlier (more specific) section.
        deduped = [c for c in candidates if c["link"] not in used_links]

        picked = select_diverse(deduped)

        for item in picked:
            used_links.add(item["link"])
            item["link"] = resolve_real_url(item["link"])
            scraped = scrape_article_paragraph(item["link"])
            if scraped and len(scraped) > max(len(item["summary"]), SCRAPE_MIN_CHARS):
                item["summary"] = scraped

        rendered_sections.append({"key": section["key"], "title": section["title"], "items": picked})

    return rendered_sections


def list_archive_dates():
    """Return sorted (newest first) ISO date strings for existing archive pages.

    Returns [] on any problem reading the directory - the archive dropdown
    is a bonus feature and must never be able to stop today's edition from
    being built.
    """
    try:
        if not os.path.isdir(ARCHIVE_DIR):
            return []
        dates = []
        for name in os.listdir(ARCHIVE_DIR):
            m = ARCHIVE_FILENAME_RE.match(name)
            if m:
                dates.append(m.group(1))
        return sorted(dates, reverse=True)
    except OSError:
        return []


def prune_old_archives(dates, today_iso):
    """Delete archive pages older than ARCHIVE_RETENTION_DAYS and return the survivors."""
    try:
        cutoff = (datetime.strptime(today_iso, "%Y-%m-%d") - timedelta(days=ARCHIVE_RETENTION_DAYS)).date()
    except ValueError:
        return dates
    kept = []
    for d in dates:
        try:
            is_old = datetime.strptime(d, "%Y-%m-%d").date() < cutoff
        except ValueError:
            continue
        if is_old:
            try:
                os.remove(os.path.join(ARCHIVE_DIR, f"{d}.html"))
            except OSError:
                pass
        else:
            kept.append(d)
    return kept


def format_archive_label(iso_date):
    try:
        return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%A, %d %B %Y")
    except ValueError:
        return iso_date


def render_archive_nav(other_dates, current_iso_date, current_is_today):
    """A single <select> that jumps to any edition. Plain onchange navigation
    only - no localStorage, no fetch, nothing that can throw at runtime.

    `other_dates` should be every known archive date; today's own date is
    filtered out here unconditionally, so it's never listed twice (once as
    the fixed "Today" option, once as a same-day archive entry).
    """
    other_dates = [d for d in other_dates if d != current_iso_date]

    options = [
        f'<option value="{html.escape(SITE_BASE_URL)}"{" selected" if current_is_today else ""}>Today</option>'
    ]
    if current_iso_date and not current_is_today:
        # This is today's own archive copy - it's the most recent entry
        # after "Today" itself, so it goes first among the dated options.
        url = f"{SITE_BASE_URL}{ARCHIVE_DIR}/{current_iso_date}.html"
        options.append(f'<option value="{html.escape(url)}" selected>{html.escape(format_archive_label(current_iso_date))}</option>')
    for d in other_dates:
        url = f"{SITE_BASE_URL}{ARCHIVE_DIR}/{d}.html"
        options.append(f'<option value="{html.escape(url)}">{html.escape(format_archive_label(d))}</option>')

    return (
        '<select onchange="if (this.value) { window.location.href = this.value; }" '
        'aria-label="Jump to a previous edition" '
        'style="margin-top:12px;font-family:Arial,Helvetica,sans-serif;font-size:13px;'
        'padding:6px 10px;border-radius:6px;border:1px solid #3a4a6a;background:#22345c;color:#fff;">'
        + "".join(options)
        + "</select>"
    )


def format_pubdate(dt):
    """UK-local, human-readable publish time - e.g. "24 Aug, 15:32"."""
    return dt.astimezone(ZoneInfo("Europe/London")).strftime("%-d %b, %H:%M")


def render_story(item, section_title):
    paywall_html = (
        '<span class="paywall">🔒 May require a subscription</span>' if item.get("paywalled") else ""
    )
    pubdate = format_pubdate(item["published"])
    tags = item.get("tags") or set()
    institutions = item.get("institutions") or set()
    badge_spans = [f'<span class="tag-badge">{html.escape(TAG_LABELS[t])}</span>' for t in TAG_ORDER if t in tags]
    badge_spans += [f'<span class="tag-badge tag-badge-inst">{html.escape(i)}</span>' for i in sorted(institutions)]
    badges_html = f'<div class="tags">{"".join(badge_spans)}</div>' if badge_spans else ""
    data_tags = html.escape(",".join(t for t in TAG_ORDER if t in tags))
    data_institutions = html.escape(",".join(sorted(institutions)))
    # <details>/<summary>: title-only by default, tap/click to expand for
    # the rest (ported from Agilisys - same native-element choice, for the
    # same reasons: zero new JS for the toggle itself, works identically on
    # mobile and desktop, free keyboard/screen-reader behaviour, and none of
    # the existing bookmark/share/filter JS needed to change since it
    # already selects by class/attribute, not tag name or DOM depth).
    #
    # data-* attributes carry everything the bookmark feature needs to
    # reconstruct this story from scratch client-side: bookmarking must
    # save the full content, not just a link, because tomorrow's rebuild
    # removes this story from index.html entirely - only localStorage (not
    # this page) will still have it. data-tags/data-institutions are also
    # read directly by the tag filter bar's JS.
    return f'''        <details class="story" data-id="{html.escape(item['link'])}" data-title="{html.escape(item['title'])}" data-summary="{html.escape(item['summary'])}" data-source="{html.escape(item['source'])}" data-published="{html.escape(item['published'].isoformat())}" data-section="{html.escape(section_title)}" data-paywalled="{"true" if item.get('paywalled') else "false"}" data-tags="{data_tags}" data-institutions="{data_institutions}">
          <summary>
            <span class="kicker">{html.escape(section_title)}</span>
            <span class="headline">{html.escape(item['title'])}</span>
          </summary>
          {badges_html}
          <p class="summary">{html.escape(item['summary'])}</p>
          <span class="source"><a href="{html.escape(item['link'])}" target="_blank" rel="noopener">{html.escape(item['source'])}</a> &middot; <span class="pubdate">{html.escape(pubdate)}</span></span>
          {paywall_html}
          <button type="button" class="share-btn" hidden data-title="{html.escape(item['title'])}" data-url="{html.escape(item['link'])}">&#8599; Share</button>
          <button type="button" class="bookmark-btn" aria-label="Bookmark this story">&#9734; Bookmark</button>
        </details>'''


def pick_top_picks(sections, n=3):
    """The n most recent stories, but from n *different* topics where
    possible - a pure top-n-by-timestamp pick tends to land on whichever
    single topic happened to break most recently (e.g. three Higher
    Education stories in a row), which isn't a useful cross-section
    snapshot for a 10-second glance. Deterministic recency ranking only,
    no AI judgment."""
    all_items = []
    for section in sections:
        for item in section["items"]:
            all_items.append((item, section["title"]))
    all_items.sort(key=lambda pair: pair[0]["published"], reverse=True)

    picks = []
    used_titles = set()

    for item, section_title in all_items:
        if len(picks) >= n:
            break
        if section_title in used_titles:
            continue
        picks.append((item, section_title))
        used_titles.add(section_title)
    return picks


def render_top_picks(sections):
    picks = pick_top_picks(sections)
    if not picks:
        return ""
    rows = "\n".join(
        f'''      <a class="pick" href="{html.escape(item['link'])}" target="_blank" rel="noopener">
        <span class="pick-topic">{html.escape(section_title)}</span>
        <span class="pick-headline">{html.escape(item['title'])}</span>
      </a>'''
        for item, section_title in picks
    )
    return f'''  <div class="top-picks">
    <div class="top-picks-label">Right now</div>
{rows}
  </div>'''


def render_filter_bar(sections):
    """A sticky row of topic chips - the Oxford Edge team reads this mostly on their phones,
    so jumping straight to one topic beats scrolling past nine sections to
    find it. Pure client-side show/hide, no page reload, no dependencies."""
    chips = ['<button type="button" class="chip active" data-topic="all">All</button>']
    for section in sections:
        chips.append(
            f'<button type="button" class="chip" data-topic="{html.escape(section["key"])}">'
            f'{html.escape(section["title"])}</button>'
        )
    return '<nav class="filter-bar">' + "".join(chips) + "</nav>"


def render_tag_filter_bar(sections):
    """Multi-select tag/institution filters, layered on top of the existing
    single-select topic chips. Only shows chips for tags/institutions that
    actually appear in today's items - a fixed chip for every one of the 10
    content tags plus 18 institutions would be mostly dead weight on a
    typical day."""
    used_tags = set()
    used_institutions = set()
    for section in sections:
        for item in section["items"]:
            used_tags |= item.get("tags") or set()
            used_institutions |= item.get("institutions") or set()

    if not used_tags and not used_institutions:
        return ""

    parts = []
    if used_tags:
        chips = "".join(
            f'<button type="button" class="tag-chip" data-tag="{html.escape(t)}">{html.escape(TAG_LABELS[t])}</button>'
            for t in TAG_ORDER if t in used_tags
        )
        parts.append(
            '<nav class="tag-filter-bar" aria-label="Filter by category">'
            '<span class="tag-filter-label">Category:</span>' + chips +
            '<button type="button" class="tag-chip tag-chip-clear" data-clear="tags">Clear</button></nav>'
        )
    if used_institutions:
        chips = "".join(
            f'<button type="button" class="tag-chip" data-institution="{html.escape(i)}">{html.escape(i)}</button>'
            for i in sorted(used_institutions)
        )
        parts.append(
            '<nav class="tag-filter-bar" aria-label="Filter by institution">'
            '<span class="tag-filter-label">Institution:</span>' + chips +
            '<button type="button" class="tag-chip tag-chip-clear" data-clear="institutions">Clear</button></nav>'
        )
    return "".join(parts)


def render_html(sections, today_str, updated_str, archive_nav_html="", asset_prefix=""):
    story_blocks = []
    for section in sections:
        if section["items"]:
            stories_html = "\n".join(render_story(item, section["title"]) for item in section["items"])
        else:
            stories_html = '        <p class="no-story">No qualifying story in the last 24 hours.</p>'

        story_blocks.append(
            f'''      <section class="section" data-topic="{html.escape(section['key'])}">
        <h2>{html.escape(section['title'])}</h2>
{stories_html}
      </section>'''
        )

    sections_html = "\n".join(story_blocks)
    filter_bar_html = render_filter_bar(sections)
    tag_filter_bar_html = render_tag_filter_bar(sections)
    top_picks_html = render_top_picks(sections)

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<script>
  // Applied before first paint so switching to dark mode once never causes
  // a flash of the light theme on every later page load.
  (function () {{
    try {{
      if (localStorage.getItem('oxfordEdgeTheme') === 'dark') {{
        document.documentElement.setAttribute('data-theme', 'dark');
      }}
    }} catch (e) {{}}
  }})();
</script>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Oxford Edge Daily Signal — {today_str}</title>
<link rel="manifest" href="{asset_prefix}manifest.webmanifest">
<link rel="icon" href="{asset_prefix}icon.png" type="image/png">
<link rel="apple-touch-icon" href="{asset_prefix}icon.png">
<meta name="theme-color" content="#1a2a4a">
<style>
  :root {{
    --navy: #1a2a4a;
    --accent: #1a2a4a;
    --cream: #f4f4f2;
    --card-bg: #ffffff;
    --text: #1a1a1a;
    --muted: #767676;
    --source: #999999;
    --rule: #d8d4c8;
    --paywall-bg: #fff4e6;
    --paywall-text: #92400e;
    --paywall-border: #f0d2a6;
  }}
  [data-theme="dark"] {{
    --navy: #24406e;
    --accent: #8fb0e8;
    --cream: #14181f;
    --card-bg: #1f2530;
    --text: #e8e8e6;
    --muted: #a8a8a8;
    --source: #8a8a8a;
    --rule: #333844;
    --paywall-bg: #3a2c14;
    --paywall-text: #f0c987;
    --paywall-border: #5a4520;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--cream);
    font-family: Georgia, 'Times New Roman', serif;
    color: var(--text);
  }}
  .masthead {{
    background: var(--navy);
    padding: 32px 20px;
    text-align: center;
  }}
  .masthead h1 {{
    margin: 0;
    color: #ffffff;
    font-size: clamp(24px, 5vw, 34px);
    font-weight: bold;
    letter-spacing: 0.5px;
  }}
  .masthead .date {{
    display: block;
    margin-top: 8px;
    color: #c9d2e3;
    font-family: Arial, Helvetica, sans-serif;
    font-size: 14px;
    letter-spacing: 0.5px;
  }}
  .masthead-controls {{
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    gap: 8px;
    margin-top: 12px;
  }}
  .masthead-controls select,
  .masthead-controls button {{
    font-family: Arial, Helvetica, sans-serif;
    font-size: 13px;
    font-weight: bold;
    padding: 6px 12px;
    border-radius: 6px;
    border: 1px solid #3a4a6a;
    background: #22345c;
    color: #fff;
    cursor: pointer;
  }}
  .bookmarks-toggle.active {{
    background: #e9b949;
    border-color: #e9b949;
    color: #1a2a4a;
  }}
  #bookmarks-view {{
    max-width: 1200px;
    margin: 0 auto;
    padding: 8px 20px 60px;
  }}
  #bookmarks-view[hidden] {{
    display: none;
  }}
  .bookmarks-empty {{
    font-family: Arial, Helvetica, sans-serif;
    font-size: 14px;
    color: var(--muted);
    font-style: italic;
    padding: 20px 0;
    text-align: center;
  }}
  .intro {{
    max-width: 1200px;
    margin: 16px auto 0;
    padding: 0 20px;
    font-family: Arial, Helvetica, sans-serif;
    font-size: 12px;
    color: var(--muted);
    font-style: italic;
    text-align: center;
  }}
  /* Wider, multi-column layout on desktop (ported from Agilisys's FT-style
     redesign) - auto-fit + minmax collapses to a single column on its own
     once the viewport is too narrow, so phones still get the same stacked
     layout as before with no separate media query needed. Oxford Edge's own
     navy/cream editorial palette is kept as-is - only the structural grid
     pattern is ported, not Agilisys's colour scheme. */
  main {{
    max-width: 1200px;
    margin: 0 auto;
    padding: 8px 20px 60px;
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
    gap: 0 40px;
    align-items: start;
  }}
  .section {{
    margin-top: 32px;
  }}
  .section h2 {{
    font-family: Arial, Helvetica, sans-serif;
    font-size: 15px;
    color: var(--accent);
    text-transform: uppercase;
    letter-spacing: 1px;
    border-bottom: 2px solid var(--accent);
    padding-bottom: 6px;
    margin: 0 0 14px;
  }}
  .story {{
    padding: 14px 0;
    border-bottom: 1px solid var(--rule);
  }}
  .story:last-child {{
    border-bottom: none;
  }}
  .story summary {{
    list-style: none;
    cursor: pointer;
  }}
  .story summary::-webkit-details-marker {{
    display: none;
  }}
  .story summary::after {{
    content: '▸ Expand';
    display: block;
    margin-top: 4px;
    font-family: Arial, Helvetica, sans-serif;
    font-size: 11px;
    font-weight: bold;
    color: var(--accent);
  }}
  .story[open] summary::after {{
    content: '▾ Collapse';
  }}
  .kicker {{
    display: block;
    font-family: Arial, Helvetica, sans-serif;
    font-size: 10px;
    font-weight: bold;
    letter-spacing: 0.5px;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 2px;
  }}
  .headline {{
    display: inline;
    font-family: Georgia, 'Times New Roman', serif;
    font-size: 17px;
    font-weight: bold;
    color: var(--accent);
    line-height: 1.35;
  }}
  .summary {{
    margin: 6px 0 8px;
    font-family: Arial, Helvetica, sans-serif;
    font-size: 14px;
    color: var(--muted);
    line-height: 1.5;
  }}
  .source {{
    font-family: Arial, Helvetica, sans-serif;
    font-size: 11px;
    font-variant: small-caps;
    letter-spacing: 0.5px;
  }}
  .source a {{
    color: var(--source);
    text-decoration: none;
  }}
  .source a:hover {{
    text-decoration: underline;
  }}
  .no-story {{
    padding: 6px 0 0;
    font-family: Arial, Helvetica, sans-serif;
    font-size: 13px;
    color: var(--source);
    font-style: italic;
  }}
  .paywall {{
    display: inline-block;
    margin-top: 6px;
    font-family: Arial, Helvetica, sans-serif;
    font-size: 11px;
    color: var(--paywall-text);
    background: var(--paywall-bg);
    border: 1px solid var(--paywall-border);
    border-radius: 4px;
    padding: 2px 7px;
  }}
  .pubdate {{
    font-family: Arial, Helvetica, sans-serif;
    font-size: 11px;
    color: var(--source);
  }}
  .share-btn, .bookmark-btn {{
    display: inline-block;
    margin-top: 8px;
    font-family: Arial, Helvetica, sans-serif;
    font-size: 12px;
    font-weight: bold;
    color: var(--accent);
    background: none;
    border: 1px solid var(--rule);
    border-radius: 999px;
    padding: 4px 12px;
    cursor: pointer;
  }}
  .bookmark-btn {{
    margin-left: 6px;
  }}
  .bookmark-btn.active {{
    background: var(--accent);
    color: var(--cream);
    border-color: var(--accent);
  }}
  .top-picks {{
    max-width: 1200px;
    margin: 16px auto 0;
    padding: 0 20px;
  }}
  .top-picks-label {{
    font-family: Arial, Helvetica, sans-serif;
    font-size: 11px;
    font-weight: bold;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: var(--muted);
    margin-bottom: 6px;
  }}
  .pick {{
    display: block;
    background: var(--card-bg);
    border: 1px solid var(--rule);
    border-left: 4px solid var(--accent);
    border-radius: 6px;
    padding: 8px 12px;
    margin-bottom: 6px;
    text-decoration: none;
    color: inherit;
  }}
  .pick-topic {{
    display: block;
    font-family: Arial, Helvetica, sans-serif;
    font-size: 10px;
    font-weight: bold;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--accent);
  }}
  .pick-headline {{
    display: block;
    font-family: Georgia, 'Times New Roman', serif;
    font-size: 15px;
    font-weight: bold;
    color: var(--text);
    margin-top: 2px;
  }}
  .filter-bar {{
    position: sticky;
    top: 0;
    z-index: 10;
    display: flex;
    gap: 8px;
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
    background: var(--cream);
    border-bottom: 1px solid var(--rule);
    padding: 10px 20px;
    scrollbar-width: none;
  }}
  .filter-bar::-webkit-scrollbar {{
    display: none;
  }}
  .chip {{
    flex: 0 0 auto;
    font-family: Arial, Helvetica, sans-serif;
    font-size: 13px;
    font-weight: bold;
    color: var(--accent);
    background: var(--card-bg);
    border: 1px solid var(--rule);
    border-radius: 999px;
    padding: 6px 14px;
    cursor: pointer;
  }}
  .chip.active {{
    background: var(--navy);
    color: #ffffff;
    border-color: var(--navy);
  }}
  .tag-filter-bar {{
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
    max-width: 1200px;
    margin: 0 auto;
    padding: 8px 20px 0;
  }}
  .tag-filter-label {{
    font-family: Arial, Helvetica, sans-serif;
    font-size: 11px;
    font-weight: bold;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--muted);
  }}
  .tag-chip {{
    font-family: Arial, Helvetica, sans-serif;
    font-size: 11px;
    font-weight: bold;
    color: var(--accent);
    background: var(--card-bg);
    border: 1px solid var(--rule);
    border-radius: 999px;
    padding: 3px 10px;
    cursor: pointer;
  }}
  .tag-chip.active {{
    background: var(--navy);
    color: #ffffff;
    border-color: var(--navy);
  }}
  .tag-chip-clear {{
    border-style: dashed;
  }}
  .tags {{
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    margin-top: 6px;
  }}
  .tag-badge {{
    font-family: Arial, Helvetica, sans-serif;
    font-size: 10px;
    font-weight: bold;
    color: var(--accent);
    background: var(--cream);
    border: 1px solid var(--rule);
    border-radius: 4px;
    padding: 2px 6px;
  }}
  .tag-badge-inst {{
    color: var(--navy);
    border-color: var(--navy);
  }}
  .story[hidden],
  .section[hidden],
  .filter-bar[hidden],
  .tag-filter-bar[hidden],
  .top-picks[hidden],
  .intro[hidden],
  main[hidden] {{
    display: none;
  }}
  footer {{
    max-width: 1200px;
    margin: 0 auto 40px;
    padding: 0 20px;
    font-family: Arial, Helvetica, sans-serif;
    font-size: 11px;
    color: var(--source);
    text-align: center;
  }}
</style>
</head>
<body>
  <div class="masthead">
    <h1>Oxford Edge Daily Signal</h1>
    <span class="date">{today_str}, Updated as of {html.escape(updated_str)}</span>
    <div class="masthead-controls">
      {archive_nav_html}
      <button type="button" id="theme-toggle" aria-label="Toggle dark mode">&#127769; Dark mode</button>
      <button type="button" id="bookmarks-toggle" class="bookmarks-toggle" aria-label="View bookmarked stories">&#128278; Bookmarks (<span id="bookmarks-count">0</span>)</button>
    </div>
  </div>
{top_picks_html}
  {filter_bar_html}
  {tag_filter_bar_html}
  <p class="intro">Higher-education and philanthropy news pulled directly from source RSS feeds. Most stories are from the last 24 hours; foundation, government-grant and regulatory/tax stories carry a slower news cycle and may be up to a week old.</p>
  <main>
{sections_html}
  </main>
  <div id="bookmarks-view" hidden></div>
  <footer>Built automatically from live RSS feeds &mdash; no AI-generated content.</footer>
  <script>
    (function () {{
      var chips = Array.prototype.slice.call(document.querySelectorAll('.chip'));
      var sections = Array.prototype.slice.call(document.querySelectorAll('.section'));
      var tagChips = Array.prototype.slice.call(document.querySelectorAll('.tag-chip[data-tag]'));
      var instChips = Array.prototype.slice.call(document.querySelectorAll('.tag-chip[data-institution]'));
      var clearBtns = Array.prototype.slice.call(document.querySelectorAll('.tag-chip-clear'));
      var activeTopic = 'all';
      var activeTags = {{}};
      var activeInstitutions = {{}};

      function hasAny(obj) {{
        for (var k in obj) {{ if (obj[k]) return true; }}
        return false;
      }}

      // Topic (section) filtering stays single-select, as before. Tag and
      // institution filtering is multi-select and operates per-story within
      // whatever sections the topic filter leaves visible - a section with
      // zero visible stories after tag filtering hides itself too, but only
      // when a tag/institution filter is actually active (otherwise a
      // genuinely empty section should still show its "no story" message).
      function applyFilters() {{
        var tagsActive = hasAny(activeTags);
        var instActive = hasAny(activeInstitutions);
        sections.forEach(function (section) {{
          var topicMatch = activeTopic === 'all' || section.getAttribute('data-topic') === activeTopic;
          var anyStoryVisible = false;
          Array.prototype.slice.call(section.querySelectorAll('.story')).forEach(function (story) {{
            var storyTags = (story.getAttribute('data-tags') || '').split(',').filter(Boolean);
            var storyInst = (story.getAttribute('data-institutions') || '').split(',').filter(Boolean);
            var tagMatch = !tagsActive || storyTags.some(function (t) {{ return activeTags[t]; }});
            var instMatch = !instActive || storyInst.some(function (i) {{ return activeInstitutions[i]; }});
            var visible = topicMatch && tagMatch && instMatch;
            story.hidden = !visible;
            if (visible) anyStoryVisible = true;
          }});
          section.hidden = !topicMatch || ((tagsActive || instActive) && !anyStoryVisible);
        }});
      }}

      chips.forEach(function (chip) {{
        chip.addEventListener('click', function () {{
          chips.forEach(function (c) {{ c.classList.remove('active'); }});
          chip.classList.add('active');
          activeTopic = chip.getAttribute('data-topic');
          applyFilters();
        }});
      }});
      tagChips.forEach(function (chip) {{
        chip.addEventListener('click', function () {{
          var tag = chip.getAttribute('data-tag');
          activeTags[tag] = !activeTags[tag];
          chip.classList.toggle('active', activeTags[tag]);
          applyFilters();
        }});
      }});
      instChips.forEach(function (chip) {{
        chip.addEventListener('click', function () {{
          var inst = chip.getAttribute('data-institution');
          activeInstitutions[inst] = !activeInstitutions[inst];
          chip.classList.toggle('active', activeInstitutions[inst]);
          applyFilters();
        }});
      }});
      clearBtns.forEach(function (btn) {{
        btn.addEventListener('click', function () {{
          var which = btn.getAttribute('data-clear');
          if (which === 'tags') {{
            activeTags = {{}};
            tagChips.forEach(function (c) {{ c.classList.remove('active'); }});
          }} else {{
            activeInstitutions = {{}};
            instChips.forEach(function (c) {{ c.classList.remove('active'); }});
          }}
          applyFilters();
        }});
      }});

      if (navigator.share) {{
        Array.prototype.slice.call(document.querySelectorAll('.share-btn')).forEach(function (btn) {{
          btn.hidden = false;
          btn.addEventListener('click', function () {{
            navigator.share({{
              title: btn.getAttribute('data-title'),
              url: btn.getAttribute('data-url')
            }}).catch(function () {{}});
          }});
        }});
      }}

      // Dark mode - persisted, applied before paint on every future load by
      // the small script at the top of <head>; this handler just flips it.
      var themeToggle = document.getElementById('theme-toggle');
      function refreshThemeLabel() {{
        var isDark = document.documentElement.getAttribute('data-theme') === 'dark';
        themeToggle.textContent = isDark ? '☀️ Light mode' : '🌙 Dark mode';
      }}
      refreshThemeLabel();
      themeToggle.addEventListener('click', function () {{
        var isDark = document.documentElement.getAttribute('data-theme') === 'dark';
        if (isDark) {{
          document.documentElement.removeAttribute('data-theme');
          try {{ localStorage.setItem('oxfordEdgeTheme', 'light'); }} catch (e) {{}}
        }} else {{
          document.documentElement.setAttribute('data-theme', 'dark');
          try {{ localStorage.setItem('oxfordEdgeTheme', 'dark'); }} catch (e) {{}}
        }}
        refreshThemeLabel();
      }});

      // Bookmarks - stores each bookmarked story's full content (not just
      // its link), because tomorrow's rebuild removes the story from this
      // page entirely; only localStorage will still have it. Shared across
      // every page on this site (today's edition, any archive date) since
      // localStorage is per-origin, not per-page.
      var BOOKMARK_KEY = 'oxfordEdgeBookmarks';

      function getBookmarks() {{
        try {{ return JSON.parse(localStorage.getItem(BOOKMARK_KEY) || '[]'); }}
        catch (e) {{ return []; }}
      }}
      function saveBookmarks(list) {{
        try {{ localStorage.setItem(BOOKMARK_KEY, JSON.stringify(list)); }} catch (e) {{}}
      }}
      function updateBookmarksCount() {{
        var el = document.getElementById('bookmarks-count');
        if (el) el.textContent = getBookmarks().length;
      }}
      function syncBookmarkButtons() {{
        var bookmarks = getBookmarks();
        Array.prototype.slice.call(document.querySelectorAll('.story')).forEach(function (story) {{
          var id = story.getAttribute('data-id');
          var btn = story.querySelector('.bookmark-btn');
          if (!btn) return;
          var bookmarked = bookmarks.some(function (b) {{ return b.id === id; }});
          btn.classList.toggle('active', bookmarked);
          btn.innerHTML = bookmarked ? '★ Bookmarked' : '☆ Bookmark';
        }});
        updateBookmarksCount();
      }}
      function toggleBookmark(story) {{
        var id = story.getAttribute('data-id');
        var bookmarks = getBookmarks();
        var idx = -1;
        for (var i = 0; i < bookmarks.length; i++) {{
          if (bookmarks[i].id === id) {{ idx = i; break; }}
        }}
        if (idx >= 0) {{
          bookmarks.splice(idx, 1);
        }} else {{
          bookmarks.unshift({{
            id: id,
            title: story.getAttribute('data-title'),
            summary: story.getAttribute('data-summary'),
            source: story.getAttribute('data-source'),
            published: story.getAttribute('data-published'),
            section: story.getAttribute('data-section'),
            paywalled: story.getAttribute('data-paywalled') === 'true'
          }});
        }}
        saveBookmarks(bookmarks);
        syncBookmarkButtons();
        if (!bookmarksView.hidden) renderBookmarksView();
      }}
      Array.prototype.slice.call(document.querySelectorAll('.bookmark-btn')).forEach(function (btn) {{
        btn.addEventListener('click', function () {{ toggleBookmark(btn.closest('.story')); }});
      }});
      syncBookmarkButtons();

      function escapeHtml(s) {{
        var div = document.createElement('div');
        div.textContent = s || '';
        return div.innerHTML;
      }}
      function renderBookmarksView() {{
        var bookmarks = getBookmarks();
        if (!bookmarks.length) {{
          bookmarksView.innerHTML = '<p class="bookmarks-empty">No bookmarks yet &mdash; tap Bookmark on any story to save it here, even after it drops off the daily page.</p>';
          return;
        }}
        bookmarksView.innerHTML = bookmarks.map(function (b) {{
          var paywallHtml = b.paywalled ? '<span class="paywall">🔒 May require a subscription</span>' : '';
          var pub = '';
          try {{
            pub = new Date(b.published).toLocaleString('en-GB', {{ day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }});
          }} catch (e) {{}}
          return '<article class="story">' +
            '<span class="pick-topic">' + escapeHtml(b.section) + '</span>' +
            '<a class="headline" href="' + escapeHtml(b.id) + '" target="_blank" rel="noopener">' + escapeHtml(b.title) + '</a>' +
            '<p class="summary">' + escapeHtml(b.summary) + '</p>' +
            '<span class="source">' + escapeHtml(b.source) + (pub ? ' &middot; <span class="pubdate">' + escapeHtml(pub) + '</span>' : '') + '</span>' +
            paywallHtml +
            '<button type="button" class="bookmark-btn active" data-bookmark-id="' + escapeHtml(b.id) + '">★ Remove bookmark</button>' +
            '</article>';
        }}).join('');
        Array.prototype.slice.call(bookmarksView.querySelectorAll('.bookmark-btn')).forEach(function (btn) {{
          btn.addEventListener('click', function () {{
            var id = btn.getAttribute('data-bookmark-id');
            saveBookmarks(getBookmarks().filter(function (b) {{ return b.id !== id; }}));
            syncBookmarkButtons();
            renderBookmarksView();
          }});
        }});
      }}

      var bookmarksToggle = document.getElementById('bookmarks-toggle');
      var bookmarksView = document.getElementById('bookmarks-view');
      var mainEl = document.querySelector('main');
      var filterBarEl = document.querySelector('.filter-bar');
      var tagFilterBarEls = Array.prototype.slice.call(document.querySelectorAll('.tag-filter-bar'));
      var topPicksEl = document.querySelector('.top-picks');
      var introEl = document.querySelector('.intro');
      bookmarksToggle.addEventListener('click', function () {{
        var showingBookmarks = !bookmarksView.hidden;
        bookmarksView.hidden = showingBookmarks;
        mainEl.hidden = !showingBookmarks;
        if (filterBarEl) filterBarEl.hidden = !showingBookmarks;
        tagFilterBarEls.forEach(function (el) {{ el.hidden = !showingBookmarks; }});
        if (topPicksEl) topPicksEl.hidden = !showingBookmarks;
        if (introEl) introEl.hidden = !showingBookmarks;
        bookmarksToggle.classList.toggle('active', !showingBookmarks);
        if (!showingBookmarks) renderBookmarksView();
      }});
    }})();
  </script>
</body>
</html>
'''


MIN_SECTIONS_WITH_CONTENT = len(SECTIONS) // 2


def main():
    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.strftime("%A, %d %B %Y")
    now_uk = now_utc.astimezone(ZoneInfo("Europe/London"))
    updated_str = now_uk.strftime("%H:%M %Z")
    sections = build_sections()
    today_iso = now_utc.strftime("%Y-%m-%d")

    # Archive dropdown setup is entirely best-effort: if anything here goes
    # wrong, today's edition must still build and publish with no dropdown,
    # never fail the whole run over a bonus feature.
    archive_nav_index = ""
    archive_nav_for_copy = ""
    try:
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        existing_dates = prune_old_archives(list_archive_dates(), today_iso)
        archive_nav_index = render_archive_nav(existing_dates, today_iso, current_is_today=True)
        archive_nav_for_copy = render_archive_nav(existing_dates, today_iso, current_is_today=False)
    except Exception as exc:  # noqa: BLE001
        print(f"  ! archive dropdown setup failed (non-fatal, continuing without it): {exc}")

    output = render_html(sections, today_str, updated_str, archive_nav_html=archive_nav_index, asset_prefix="")
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(output)

    if archive_nav_for_copy:
        try:
            archive_copy = render_html(
                sections, today_str, updated_str, archive_nav_html=archive_nav_for_copy, asset_prefix="../"
            )
            with open(os.path.join(ARCHIVE_DIR, f"{today_iso}.html"), "w", encoding="utf-8") as f:
                f.write(archive_copy)
        except OSError as exc:
            print(f"  ! failed to write today's archive copy (non-fatal): {exc}")

    # Console summary for local testing / CI logs.
    for section in sections:
        print(f"\n=== {section['title']} ({len(section['items'])} item(s)) ===")
        if not section["items"]:
            print("  (no qualifying story in the last 24 hours)")
        for item in section["items"]:
            print(f"  - {item['title']}")
            print(f"    published: {item['published'].isoformat()}")
            print(f"    link: {item['link']}")

    # Sanity check: a single feed going quiet is normal and expected (that
    # section just says so honestly). Most sections going empty at once is
    # not normal - it means something broke pipeline-wide (feedparser,
    # network, a shared bug), not that the news dried up. Fail loudly so
    # GitHub's automatic failure email fires and this deploy is blocked,
    # leaving yesterday's good page live instead of publishing a near-empty
    # briefing.
    sections_with_content = sum(1 for s in sections if s["items"])
    if sections_with_content < MIN_SECTIONS_WITH_CONTENT:
        raise SystemExit(
            f"Only {sections_with_content}/{len(sections)} sections had any "
            f"qualifying stories (need at least {MIN_SECTIONS_WITH_CONTENT}) - "
            "this looks like a pipeline-wide failure, not a quiet news day. "
            "Failing the build so the last good deploy stays live."
        )


if __name__ == "__main__":
    main()
