import unittest
from unittest.mock import Mock, patch
from contextlib import redirect_stdout
from io import StringIO

import main


class ReadFeedTests(unittest.TestCase):
    def test_prefers_full_content_from_xml_feed(self):
        response = Mock()
        response.read.return_value = b"""\
            <rss xmlns:content="http://purl.org/rss/1.0/modules/content/">
              <channel><item>
                <title>A story</title>
                <link>https://example.com/story</link>
                <description>Short summary</description>
                <content:encoded>&lt;p&gt;Full article text&lt;/p&gt;</content:encoded>
              </item></channel>
            </rss>
        """
        response_context = Mock()
        response_context.__enter__ = Mock(return_value=response)
        response_context.__exit__ = Mock(return_value=False)

        with patch.object(main.urllib.request, "urlopen", return_value=response_context):
            articles = main.read_feed("Example", "https://example.com/feed", timeout=1)

        self.assertEqual(articles[0]["summary"], "Full article text")

    def test_prints_feed_content_in_terminal(self):
        output = StringIO()
        article = {
            "title": "A story",
            "link": "https://example.com/story",
            "summary": "Readable article text",
        }

        with (
            patch("sys.argv", ["main.py"]),
            patch.object(main, "FEEDS", (("Example", "https://example.com/feed"),)),
            patch.object(main, "load_saved_feeds", return_value=[]),
            patch.object(main, "read_feed", return_value=[article]),
            redirect_stdout(output),
        ):
            result = main.main()

        self.assertEqual(result, 0)
        self.assertIn("Readable article text", output.getvalue())


class BrowseFeedsTests(unittest.TestCase):
    def test_selecting_story_fetches_and_displays_article_text(self):
        responses = iter(("1", "1", "0", "0"))
        output = []
        articles = [
            {"title": "A story", "link": "https://example.com/story", "summary": "Summary"}
        ]

        with (
            patch.object(main, "read_feed", return_value=articles),
            patch.object(main, "read_article", return_value="Full article text") as read_article,
        ):
            main.browse_feeds(
                [("Example", "https://example.com/feed")],
                timeout=1,
                limit=10,
                input_fn=lambda _: next(responses),
                output=output.append,
            )

        read_article.assert_called_once_with("https://example.com/story", 1)
        self.assertIn("1. A story", output)
        self.assertIn("   Summary", output)
        self.assertIn("\nA story\n\nFull article text", output)


class TerminalUiTests(unittest.TestCase):
    def test_search_select_feed_and_open_story(self):
        class FakeScreen:
            def __init__(self):
                self.keys = iter(("a", "i", "\n", "\n", "\n", "\x1b", "q"))

            def getmaxyx(self):
                return 20, 80

            def get_wch(self):
                return next(self.keys)

            def erase(self):
                pass

            def addnstr(self, *args):
                pass

            def hline(self, *args):
                pass

            def refresh(self):
                pass

            def keypad(self, enabled):
                pass

        with (
            patch.object(
                main,
                "search_feeds",
                return_value=[{"name": "Example", "url": "https://example.com/feed"}],
            ) as search_feeds,
            patch.object(
                main,
                "read_feed",
                return_value=[
                    {"title": "A story", "link": "https://example.com/story", "summary": ""}
                ],
            ) as read_feed,
            patch.object(main, "read_article", return_value="Full article text") as read_article,
            patch.object(main.curses, "ACS_HLINE", 0, create=True),
        ):
            main._terminal_ui(FakeScreen(), [], timeout=1, limit=10)

        search_feeds.assert_called_once_with("ai", 1)
        read_feed.assert_called_once_with("Example", "https://example.com/feed", 1)
        read_article.assert_called_once_with("https://example.com/story", 1)


class ReadArticleTests(unittest.TestCase):
    def test_extracts_readable_text_and_skips_scripts_and_navigation(self):
        page = """
            <html><body>
              <nav>Site navigation</nav>
              <article><h1>A story</h1><p>First &amp; foremost.</p>
              <p>Second paragraph.</p><script>hidden()</script></article>
            </body></html>
        """

        with patch.object(main, "_fetch", return_value=page):
            text = main.read_article("https://example.com/story", timeout=1)

        self.assertEqual(text, "A story\n\nFirst & foremost.\n\nSecond paragraph.")


if __name__ == "__main__":
    unittest.main()
