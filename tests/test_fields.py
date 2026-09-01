import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reddit_alpha.fields import COMMENT_FIELDS, POST_FIELDS, project_comment, project_post


def test_drops_the_heavy_unused_fields():
    """These were measured as the bulk of the archive and are never read."""
    post = {"id": "a", "created_utc": 1, "title": "t",
            "all_awardings": [{"x": 1}] * 50, "preview": {"images": [1, 2, 3]},
            "link_flair_richtext": [{"e": "text"}], "media": {"oembed": {}}}
    out = project_post(post)
    for dropped in ("all_awardings", "preview", "link_flair_richtext", "media"):
        assert dropped not in out


def test_keeps_everything_analysis_needs():
    post = {f: "v" for f in POST_FIELDS}
    assert set(project_post(post)) == POST_FIELDS
    comment = {f: "v" for f in COMMENT_FIELDS}
    assert set(project_comment(comment)) == COMMENT_FIELDS


def test_keeps_fields_the_collector_adds():
    """Derived tags such as _thread_type are not Reddit fields but must survive."""
    out = project_comment({"id": "a", "body": "x", "_thread_type": "moves_tomorrow",
                           "_subreddit": "wallstreetbets", "junk": 1})
    assert out["_thread_type"] == "moves_tomorrow"
    assert out["_subreddit"] == "wallstreetbets"
    assert "junk" not in out


def test_missing_fields_are_not_invented():
    """A record lacking a field must not gain a null one -- that inflates storage."""
    assert project_post({"id": "a", "created_utc": 1}) == {"id": "a", "created_utc": 1}


def test_body_and_selftext_are_kept():
    """The text is the entire point; losing it would be silent and fatal."""
    assert project_comment({"body": "GME 🚀"})["body"] == "GME 🚀"
    assert project_post({"selftext": "my thesis"})["selftext"] == "my thesis"
