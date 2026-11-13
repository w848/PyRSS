import argparse
import contextlib
import curses
from html.parser import HTMLParser
import html
import io
import json
import re
import sys
import textwrap
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET


FEEDS = (
    ("r/technology (top today)", "https://www.reddit.com/r/technology/top/.rss?t=day&limit=25"),
    ("Hacker News (popular)", "https://hnrss.org/frontpage?points=100"),
)
SAVED_FEEDS = Path.home() / ".tech-news-reader-feeds.json"
SUBSCRIPTIONS = Path.home() / ".tech-news-reader-subscriptions.json"
READ_ARTICLES = Path.home() / ".tech-news-reader-read.json"


class SearchResultsParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results = []
        self._href = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "a" and "result__a" in attributes.get("class", "").split():
            self._href = attributes.get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.results.append((self._href, _clean_text(" ".join(self._text))))
            self._href = None


class FeedLinksParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self._in_title = False
        self._title_text = []
        self.feeds = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "link":
            rel = attributes.get("rel", "").lower().split()
            feed_type = attributes.get("type", "").lower()
            if "alternate" in rel and any(kind in feed_type for kind in ("rss", "atom")):
                href = attributes.get("href")
                if href:
                    self.feeds.append((href, attributes.get("title", "")))

    def handle_data(self, data):
        if self._in_title:
            self._title_text.append(data)

    def handle_endtag(self, tag):
        if tag == "title" and self._in_title:
            self.title = _clean_text(" ".join(self._title_text))
            self._in_title = False


class ArticleTextParser(HTMLParser):
    BLOCK_TAGS = {
        "article", "blockquote", "br", "dd", "div", "dl", "dt", "h1", "h2",
        "h3", "h4", "h5", "h6", "li", "ol", "p", "section", "ul",
    }
    SKIP_TAGS = {"aside", "button", "footer", "form", "header", "nav", "script", "style", "svg"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        elif not self._skip_depth and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif not self._skip_depth and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip_depth:
            self.parts.append(data)


def _text(element, *names):
    for name in names:
        child = element.find(name)
        if child is not None:
            value = "".join(child.itertext()).strip()
            if value:
                return value
    return ""


def _clean_text(value):
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def _fetch(url, timeout):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            )
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def _web_result_url(href):
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc:
        href = parse_qs(parsed.query).get("uddg", [href])[0]
    return href


def search_feeds(query, timeout, result_limit=10):
    search_url = "https://html.duckduckgo.com/html/?" + urlencode({"q": query})
    parser = SearchResultsParser()
    parser.feed(_fetch(search_url, timeout))

    discovered = []
    seen = set()
    for href, result_title in parser.results[:result_limit]:
        page_url = _web_result_url(href)
        if urlparse(page_url).scheme not in ("http", "https"):
            continue
        if urlparse(page_url).path.lower().endswith((".rss", ".atom", ".xml")):
            candidates = [(page_url, result_title)]
        else:
            try:
                page_parser = FeedLinksParser()
                page_parser.feed(_fetch(page_url, timeout))
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                print(f"Skipping {page_url}: {error}", file=sys.stderr)
                continue
            candidates = [
                (urljoin(page_url, feed_url), title or page_parser.title or result_title)
                for feed_url, title in page_parser.feeds
            ]

        for feed_url, title in candidates:
            if urlparse(feed_url).scheme in ("http", "https") and feed_url not in seen:
                seen.add(feed_url)
                discovered.append({"name": title or feed_url, "url": feed_url})
    return discovered


def load_saved_feeds():
    if not SAVED_FEEDS.exists():
        return []
    with SAVED_FEEDS.open(encoding="utf-8") as file:
        saved = json.load(file)
    if not isinstance(saved, list) or any(
        not isinstance(feed, dict)
        or not isinstance(feed.get("name"), str)
        or not isinstance(feed.get("url"), str)
        for feed in saved
    ):
        raise ValueError(f"Invalid saved feeds file: {SAVED_FEEDS}")
    return [(feed["name"], feed["url"]) for feed in saved]


def load_subscriptions():
    if SUBSCRIPTIONS.exists():
        with SUBSCRIPTIONS.open(encoding="utf-8") as file:
            saved = json.load(file)
        if not isinstance(saved, list) or any(
            not isinstance(feed, dict)
            or not isinstance(feed.get("name"), str)
            or not isinstance(feed.get("url"), str)
            for feed in saved
        ):
            raise ValueError(f"Invalid subscriptions file: {SUBSCRIPTIONS}")
        feeds = [(feed["name"], feed["url"]) for feed in saved]
    else:
        feeds = [*FEEDS, *load_saved_feeds()]

    unique = []
    seen = set()
    for name, url in feeds:
        if url not in seen:
            unique.append((name, url))
            seen.add(url)
    return unique


def save_subscriptions(feeds):
    saved = [{"name": name, "url": url} for name, url in feeds]
    SUBSCRIPTIONS.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")


def load_read_articles():
    if not READ_ARTICLES.exists():
        return {}
    with READ_ARTICLES.open(encoding="utf-8") as file:
        saved = json.load(file)
    if not isinstance(saved, dict) or any(
        not isinstance(feed_url, str)
        or not isinstance(links, list)
        or any(not isinstance(link, str) for link in links)
        for feed_url, links in saved.items()
    ):
        raise ValueError(f"Invalid read articles file: {READ_ARTICLES}")
    return {feed_url: set(links) for feed_url, links in saved.items()}


def save_read_articles(read_articles):
    saved = {feed_url: sorted(links) for feed_url, links in read_articles.items()}
    READ_ARTICLES.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")


def save_feeds(feeds):
    existing = [
        {"name": name, "url": url} for name, url in load_saved_feeds()
    ]
    known_urls = {feed["url"] for feed in existing}
    for feed in feeds:
        if feed["url"] not in known_urls:
            existing.append(feed)
            known_urls.add(feed["url"])
    SAVED_FEEDS.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")


def read_feed(name, url, timeout):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "TechNewsReader/1.0 (RSS reader)"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        root = ET.fromstring(response.read())

    entries = root.findall(".//item") or root.findall(".//{*}entry")
    articles = []
    for entry in entries:
        title = _text(entry, "title", "{*}title")
        link = _text(entry, "link", "{*}link")
        if not link:
            link_element = entry.find("{*}link")
            if link_element is not None:
                link = link_element.get("href", "")
        summary = _text(
            entry,
            "{*}encoded",
            "{*}content",
            "description",
            "{*}description",
            "{*}summary",
        )
        if title and link:
            articles.append(
                {"title": _clean_text(title), "link": link, "summary": _clean_text(summary)}
            )
    return articles


def read_article(url, timeout):
    parser = ArticleTextParser()
    parser.feed(_fetch(url, timeout))
    paragraphs = [
        re.sub(r"\s+", " ", paragraph).strip()
        for paragraph in "".join(parser.parts).splitlines()
    ]
    return "\n\n".join(paragraph for paragraph in paragraphs if paragraph)


def browse_feeds(
    feeds, timeout, limit, input_fn=input, output=print
):
    while True:
        output("\nFeeds")
        for index, (name, _) in enumerate(feeds, start=1):
            output(f"{index}. {name}")
        output("0. Quit")
        selection = input_fn("Choose a feed: ").strip()
        if selection == "0":
            return
        if not selection.isdigit() or not 1 <= int(selection) <= len(feeds):
            output("Choose a number from the list.")
            continue

        name, url = feeds[int(selection) - 1]
        try:
            articles = read_feed(name, url, timeout)[:limit]
        except (urllib.error.URLError, ET.ParseError, TimeoutError, OSError) as error:
            output(f"Could not load feed: {error}")
            continue
        if not articles:
            output("No stories found.")
            continue

        while True:
            output(f"\n{name}")
            for index, article in enumerate(articles, start=1):
                output(f"{index}. {article['title']}")
                if article["summary"]:
                    output(f"   {article['summary']}")
            output("0. Back to feeds")
            selection = input_fn("Choose a story to open: ").strip()
            if selection == "0":
                break
            if not selection.isdigit() or not 1 <= int(selection) <= len(articles):
                output("Choose a number from the list.")
                continue
            article = articles[int(selection) - 1]
            try:
                text = read_article(article["link"], timeout)
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                output(f"Could not load story: {error}")
                continue
            if text:
                output(f"\n{article['title']}\n\n{text}")
            else:
                output("No readable article text found.")


def _draw_text(screen, y, x, text, width, attributes=0):
    if width > 0:
        screen.addnstr(y, x, text, width, attributes)


def _terminal_ui(screen, feeds, timeout, limit, read_articles):
    height, width = screen.getmaxyx()
    if height < 8 or width < 30:
        screen.addnstr(0, 0, "Resize the terminal to at least 30x8.", max(1, width - 1))
        screen.refresh()
        screen.getch()
        return

    split = max(4, height // 2)
    subscriptions = list(feeds)
    feed_items = subscriptions
    showing_search_results = False
    feed_index = 0
    story_index = 0
    search_text = ""
    stories = []
    active_feed_url = None
    cached_stories = {}
    article_lines = None
    scroll = 0
    focus = "search"
    status = "Search feeds or select a subscription. Tab moves between panes."
    screen.keypad(True)

    def redraw():
        screen.erase()
        _draw_text(screen, 0, 1, "Feed Reader", width - 2)
        search_attr = curses.A_REVERSE if focus == "search" else 0
        _draw_text(screen, 1, 1, f"Search: {search_text}", width - 2, search_attr)
        screen.hline(split - 1, 0, curses.ACS_HLINE, width)

        feed_title = "Search results" if showing_search_results else "Subscriptions"
        _draw_text(screen, 2, 1, feed_title, width - 2)
        visible_feeds = max(1, split - 4)
        feed_start = max(0, min(feed_index - visible_feeds + 1, len(feed_items) - visible_feeds))
        for offset, (name, url) in enumerate(feed_items[feed_start:feed_start + visible_feeds]):
            index = feed_start + offset
            attr = curses.A_REVERSE if focus == "feeds" and index == feed_index else 0
            subscribed = url in {feed_url for _, feed_url in subscriptions}
            unread = sum(
                story["link"] not in read_articles.get(url, set())
                for story in cached_stories.get(url, [])
            )
            marker = f" [{unread}]" if url in cached_stories else ""
            label = "[*]" if subscribed else "[+]"
            _draw_text(
                screen, 3 + offset, 2, f"{index + 1}. {label} {name}{marker}",
                width - 4, attr,
            )

        if article_lines is not None:
            article_title = stories[story_index]["title"]
            _draw_text(screen, split, 1, f"Article: {article_title} (Esc to return)", width - 2)
            visible_stories = max(1, height - split - 3)
            for offset, line in enumerate(article_lines[scroll:scroll + visible_stories]):
                _draw_text(screen, split + 1 + offset, 2, line, width - 4)
        else:
            _draw_text(screen, split, 1, "Stories", width - 2)
            if feed_items:
                _draw_text(screen, split + 1, 2, f"RSS: {feed_items[feed_index][1]}", width - 4)
            if not stories:
                hint = "Select a feed and press Enter to refresh it." if feed_items else "Search for a topic above."
                _draw_text(screen, split + 2, 2, hint, width - 4)
            else:
                visible_stories = max(1, height - split - 4)
                story_start = max(0, min(story_index - visible_stories + 1, len(stories) - visible_stories))
                for offset, story in enumerate(stories[story_start:story_start + visible_stories]):
                    index = story_start + offset
                    attr = curses.A_REVERSE if focus == "stories" and index == story_index else 0
                    marker = " " if story["link"] in read_articles.get(active_feed_url, set()) else "*"
                    _draw_text(
                        screen, split + 2 + offset, 2,
                        f"{index + 1}. {marker} {story['title']}", width - 4, attr,
                    )

        _draw_text(
            screen, height - 1, 0,
            "Tab panes | Enter select/open | s subscribe | x remove | m read/unread | * unread | / search | q quit",
            width - 1,
        )
        if status:
            _draw_text(screen, height - 2, 0, status, width - 1)
        screen.refresh()

    while True:
        redraw()
        key = screen.get_wch()
        if isinstance(key, int) and key == curses.KEY_RESIZE:
            height, width = screen.getmaxyx()
            split = max(4, height // 2)
            continue

        if article_lines is not None:
            visible_stories = max(1, height - split - 3)
            if key == "\x03":
                return
            if key in ("\x1b", "q"):
                article_lines = None
                status = "Choose another story or use Tab to switch panes."
            elif key in ("KEY_UP",) or (isinstance(key, int) and key == curses.KEY_UP):
                scroll = max(0, scroll - 1)
            elif key in ("KEY_DOWN",) or (isinstance(key, int) and key == curses.KEY_DOWN):
                scroll = min(max(0, len(article_lines) - visible_stories), scroll + 1)
            elif isinstance(key, int) and key == curses.KEY_PPAGE:
                scroll = max(0, scroll - visible_stories)
            elif isinstance(key, int) and key == curses.KEY_NPAGE:
                scroll = min(max(0, len(article_lines) - visible_stories), scroll + visible_stories)
            continue

        if key == "\x03" or (key == "q" and focus != "search"):
            return
        if key in ("\t",):
            focus = {"search": "feeds", "feeds": "stories", "stories": "search"}[focus]
            continue
        if key == "b" and focus != "search":
            feed_items = subscriptions
            showing_search_results = False
            feed_index = 0
            stories = []
            active_feed_url = None
            focus = "feeds"
            status = "Showing your subscriptions."
            continue
        if focus == "feeds" and key in ("s", "x") and feed_items:
            name, url = feed_items[feed_index]
            subscribed_urls = {feed_url for _, feed_url in subscriptions}
            if key == "s":
                if url in subscribed_urls:
                    status = f"{name} is already subscribed."
                    continue
                updated = [*subscriptions, (name, url)]
                try:
                    save_subscriptions(updated)
                except OSError as error:
                    status = f"Could not save subscription: {error}"
                    continue
                subscriptions[:] = updated
                status = f"Subscribed to {name}."
            else:
                if url not in subscribed_urls:
                    status = f"{name} is not in your subscriptions."
                    continue
                updated = [(feed_name, feed_url) for feed_name, feed_url in subscriptions if feed_url != url]
                try:
                    save_subscriptions(updated)
                except OSError as error:
                    status = f"Could not save subscriptions: {error}"
                    continue
                subscriptions[:] = updated
                if not showing_search_results:
                    feed_index = min(feed_index, max(0, len(feed_items) - 1))
                if active_feed_url == url:
                    stories = []
                    active_feed_url = None
                status = f"Unsubscribed from {name}."
            continue
        if focus == "stories" and key == "m" and stories and active_feed_url:
            story = stories[story_index]
            links = read_articles.setdefault(active_feed_url, set())
            was_read = story["link"] in links
            if was_read:
                links.remove(story["link"])
            else:
                links.add(story["link"])
            try:
                save_read_articles(read_articles)
            except OSError as error:
                if was_read:
                    links.add(story["link"])
                else:
                    links.remove(story["link"])
                status = f"Could not save read status: {error}"
            else:
                status = "Marked unread." if was_read else "Marked read."
            continue
        if key == "/":
            focus = "search"
            continue
        if key in ("KEY_UP", "KEY_DOWN") or (
            isinstance(key, int) and key in (curses.KEY_UP, curses.KEY_DOWN)
        ):
            direction = -1 if key in ("KEY_UP", curses.KEY_UP) else 1
            if focus == "feeds" and feed_items:
                feed_index = max(0, min(len(feed_items) - 1, feed_index + direction))
                stories = []
                active_feed_url = None
                story_index = 0
            elif focus == "stories" and stories:
                story_index = max(0, min(len(stories) - 1, story_index + direction))
            continue

        if isinstance(key, int):
            if key in (curses.KEY_BACKSPACE, 127, 8) and focus == "search":
                search_text = search_text[:-1]
                continue
            continue

        if key in ("\n", "\r"):
            if focus == "search":
                if not search_text.strip():
                    status = "Enter a search word or phrase first."
                    continue
                status = f"Searching the web for RSS feeds matching {search_text!r}..."
                redraw()
                warnings = io.StringIO()
                try:
                    with contextlib.redirect_stderr(warnings):
                        results = search_feeds(search_text.strip(), timeout)
                except (urllib.error.URLError, TimeoutError, OSError) as error:
                    status = f"Search failed: {error}"
                    continue
                feed_items = [(feed["name"], feed["url"]) for feed in results]
                showing_search_results = True
                feed_index = 0
                stories = []
                active_feed_url = None
                focus = "feeds"
                status = (
                    f"Found {len(feed_items)} feed(s). Press s to subscribe."
                    if feed_items else "No RSS or Atom feeds found."
                )
                if warnings.getvalue():
                    status += " Some websites could not be checked."
            elif focus == "feeds" and feed_items:
                name, url = feed_items[feed_index]
                try:
                    stories = read_feed(name, url, timeout)[:limit]
                    active_feed_url = url
                    cached_stories[url] = stories
                    story_index = 0
                    focus = "stories"
                    status = f"Loaded {len(stories)} stories from {name}." if stories else "This feed has no stories."
                except (urllib.error.URLError, ET.ParseError, TimeoutError, OSError) as error:
                    status = f"Could not load feed: {error}"
            elif focus == "stories" and stories:
                story = stories[story_index]
                try:
                    text = read_article(story["link"], timeout)
                except (urllib.error.URLError, TimeoutError, OSError) as error:
                    status = f"Could not load story: {error}"
                    continue
                if active_feed_url:
                    read_articles.setdefault(active_feed_url, set()).add(story["link"])
                    try:
                        save_read_articles(read_articles)
                    except OSError as error:
                        status = f"Could not save read status: {error}"
                wrapped = []
                for paragraph in text.splitlines():
                    wrapped.extend(textwrap.wrap(paragraph, max(1, width - 5)) or [""])
                    wrapped.append("")
                article_lines = wrapped or ["No readable article text found."]
                scroll = 0
            continue

        if focus == "search" and key.isprintable():
            search_text += key
            continue


def run_terminal_ui(feeds, timeout, limit, read_articles):
    curses.wrapper(_terminal_ui, feeds, timeout, limit, read_articles)


def main():
    parser = argparse.ArgumentParser(
        description="Find, subscribe to, and read RSS feeds."
    )
    parser.add_argument(
        "--search", metavar="QUERY", help="find RSS feeds on the web for a topic"
    )
    parser.add_argument(
        "--save-search",
        action="store_true",
        help="save discovered feeds for future reading (use with --search)",
    )
    parser.add_argument(
        "-n", "--limit", type=int, default=10, help="stories to show per feed (default: 10)"
    )
    parser.add_argument(
        "--timeout", type=float, default=10, help="request timeout in seconds (default: 10)"
    )
    parser.add_argument(
        "--interactive", action="store_true", help="open the split-pane feed reader"
    )
    args = parser.parse_args()
    if args.limit < 1 or args.timeout <= 0:
        parser.error("--limit must be positive and --timeout must be greater than zero")
    if args.save_search and not args.search:
        parser.error("--save-search requires --search")

    if args.search:
        try:
            feeds = search_feeds(args.search, args.timeout)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            print(f"Could not search the web: {error}", file=sys.stderr)
            return 1
        if not feeds:
            print("No RSS or Atom feeds found.")
            return 0
        for index, feed in enumerate(feeds, start=1):
            print(f"{index}. {feed['name']}\n   {feed['url']}")
        if args.save_search:
            try:
                subscriptions = load_subscriptions()
                known_urls = {url for _, url in subscriptions}
                subscriptions.extend(
                    (feed["name"], feed["url"])
                    for feed in feeds
                    if feed["url"] not in known_urls
                )
                save_subscriptions(subscriptions)
            except (OSError, json.JSONDecodeError, ValueError) as error:
                print(f"Could not save subscriptions: {error}", file=sys.stderr)
                return 1
            print(f"\nSaved subscriptions to {SUBSCRIPTIONS}")
        return 0

    found_articles = False
    try:
        feeds = load_subscriptions()
    except (OSError, json.JSONDecodeError, ValueError) as error:
        print(f"Could not load subscriptions: {error}", file=sys.stderr)
        return 1
    if args.interactive or (sys.stdin.isatty() and sys.stdout.isatty()):
        try:
            read_articles = load_read_articles()
        except (OSError, json.JSONDecodeError, ValueError) as error:
            print(f"Could not load read status: {error}", file=sys.stderr)
            return 1
        run_terminal_ui(feeds, args.timeout, args.limit, read_articles)
        return 0

    for name, url in feeds:
        print(f"\n{name}")
        print("-" * len(name))
        try:
            articles = read_feed(name, url, args.timeout)
        except (urllib.error.URLError, ET.ParseError, TimeoutError, OSError) as error:
            print(f"Could not load feed: {error}", file=sys.stderr)
            continue

        for index, article in enumerate(articles[: args.limit], start=1):
            found_articles = True
            print(f"{index}. {article['title']}\n   {article['link']}")
            if article["summary"]:
                print(f"   {article['summary']}")

    if not found_articles:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
