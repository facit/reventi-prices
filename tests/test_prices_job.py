"""Tests for prices_job.py. No network: every response comes from tests/fixtures."""
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import prices_job as job

FIXTURES = Path(__file__).parent / "fixtures"
LAST_UPDATED = b"2026-11-19T20:04:34+0000\n"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class FakeSources:
    """Answers TCGCSV and ECB URLs from the fixtures and records each URL."""

    def __init__(self):
        self.urls = []
        self.responses = {
            f"{job.TCGCSV}/last-updated.txt": LAST_UPDATED,
            f"{job.TCGCSV}/tcgplayer/79/groups": fixture("groups_79.json"),
            f"{job.TCGCSV}/tcgplayer/79/24812/prices": fixture("prices_24812.json"),
            f"{job.TCGCSV}/tcgplayer/79/23406/prices": fixture("prices_23406.json"),
            # 79/24999 is not here: a listed group with no price file answers 404.
            f"{job.TCGCSV}/tcgplayer/92/groups": fixture("groups_92.json"),
            f"{job.TCGCSV}/tcgplayer/92/24881/prices": fixture("prices_24881.json"),
            job.ECB_URL: fixture("eurofxref-daily.xml"),
        }

    def get(self, url: str) -> bytes:
        self.urls.append(url)
        response = self.responses.get(url, 404)
        if isinstance(response, int):
            raise job.HttpStatusError(url, response)
        return response


class TempDirs(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name) / "public"
        self.previous = Path(tmp.name) / "previous"
        self.sources = FakeSources()
        self.sleeps = []

    def build(self, force=False):
        job.build(self.out, self.previous, force, get=self.sources.get, sleep=self.sleeps.append)

    def publish(self):
        """Make the built files the published ones, as a deploy does."""
        for rel in job.published_paths():
            (self.previous / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.previous / rel).write_bytes((self.out / rel).read_bytes())

    def edit(self, folder: Path, rel: str, change) -> None:
        doc = json.loads((folder / rel).read_bytes())
        change(doc)
        job.write_json(folder / rel, doc)


class ParsingTests(unittest.TestCase):
    def test_last_updated_becomes_utc(self):
        self.assertEqual(job.parse_last_updated(LAST_UPDATED), "2026-11-19T20:04:34Z")
        self.assertEqual(job.parse_last_updated(b"2026-11-19T22:04:34+0200"), "2026-11-19T20:04:34Z")

    def test_last_updated_not_readable(self):
        self.assertIsNone(job.parse_last_updated(b"<html>error</html>"))

    def test_envelope_returns_the_results(self):
        groups = job.parse_envelope(fixture("groups_79.json"), "groups")
        self.assertEqual([group["groupId"] for group in groups], [24812, 23406, 24999])

    def test_envelope_accepts_prices_without_total_items(self):
        self.assertEqual(len(job.parse_envelope(fixture("prices_24812.json"), "prices")), 3)

    def test_envelope_rejects_bad_responses(self):
        bad = {
            "not JSON": b"<Error><Code>NoSuchKey</Code></Error>",
            "success is not true": b'{"success": false, "errors": ["x"], "results": []}',
            "no results list": b'{"success": true, "errors": []}',
            "totalItems is 34": b'{"totalItems": 34, "success": true, "errors": [], "results": [{"groupId": 1}]}',
            "success is not true ": b"[]",
        }
        for message, data in bad.items():
            with self.subTest(message), self.assertRaisesRegex(job.JobError, message.strip()):
                job.parse_envelope(data, "what")

    def test_ecb_date_and_rates(self):
        date, rates = job.parse_ecb(fixture("eurofxref-daily.xml"))
        self.assertEqual(date, "2026-11-19")
        self.assertEqual(rates, {"USD": 1.0992, "JPY": 175.84, "GBP": 0.83581, "SEK": 11.147})

    def test_ecb_rejects_bad_responses(self):
        for data in (b"not xml", b"<Envelope/>", fixture("eurofxref-daily.xml").replace(b"11.1470", b"n/a")):
            with self.subTest(data[:12]), self.assertRaises(job.JobError):
                job.parse_ecb(data)


class BuildTests(TempDirs):
    def test_game_file_keeps_the_tcgcsv_rows_unchanged(self):
        self.build()
        doc = json.loads((self.out / "tcgplayer/79.json").read_bytes())
        self.assertEqual(list(doc), ["version", "generatedAt", "source", "category", "groups", "prices"])
        self.assertEqual(doc["version"], 1)
        self.assertEqual(doc["category"], 79)
        self.assertEqual(
            doc["source"],
            {"name": "TCGCSV", "url": "https://tcgcsv.com/tcgplayer/79/groups", "lastUpdated": "2026-11-19T20:04:34Z"},
        )
        self.assertTrue(job.is_utc_time(doc["generatedAt"]))
        self.assertEqual(doc["groups"], json.loads(fixture("groups_79.json"))["results"])
        self.assertEqual(doc["prices"]["24812"], json.loads(fixture("prices_24812.json"))["results"])
        # The seven fields that the apps parse, with null kept as null.
        self.assertEqual(
            doc["prices"]["24812"][1],
            {"productId": 652001, "lowPrice": 0.35, "midPrice": 0.75, "highPrice": 9.99, "marketPrice": 0.62,
             "directLowPrice": 0.5, "subTypeName": "Foil"},
        )
        self.assertIsNone(doc["prices"]["24812"][2]["marketPrice"])

    def test_every_listed_group_has_a_prices_key(self):
        self.build()
        doc = json.loads((self.out / "tcgplayer/79.json").read_bytes())
        # 23406 has an empty price file; 24999 has no price file (404).
        self.assertEqual(doc["prices"]["23406"], [])
        self.assertEqual(doc["prices"]["24999"], [])
        self.assertEqual(list(doc["prices"]), ["24812", "23406", "24999"])

    def test_fx_file(self):
        self.build()
        doc = json.loads((self.out / "fx/eur.json").read_bytes())
        self.assertEqual(doc["date"], "2026-11-19")
        self.assertEqual(doc["base"], "EUR")
        self.assertEqual(doc["rates"]["SEK"], 11.147)
        self.assertEqual(doc["source"], {"name": "European Central Bank", "url": job.ECB_URL})

    def test_output_is_compact(self):
        self.build()
        for rel in job.published_paths():
            text = (self.out / rel).read_text(encoding="utf-8")
            self.assertNotIn("\n", text, rel)
            self.assertNotIn('": ', text, rel)
            self.assertNotIn(', "', text, rel)

    def test_one_request_for_each_file_with_a_pause_between(self):
        self.build()
        tcgcsv = [url for url in self.sources.urls if url.startswith(job.TCGCSV)]
        # last-updated twice, 2 group lists, 3 + 1 price files.
        self.assertEqual(len(tcgcsv), 8)
        self.assertEqual(len(set(tcgcsv)), 7)
        self.assertEqual(self.sleeps, [job.SPACING_S] * 7)

    def test_built_files_pass_validation(self):
        self.build()
        self.assertEqual(job.validate(self.out, self.previous), [])
        self.assertTrue((self.out / "index.html").is_file())

    def test_server_error_on_a_group_fails_the_build(self):
        self.sources.responses[f"{job.TCGCSV}/tcgplayer/79/23406/prices"] = 503
        with self.assertRaisesRegex(job.JobError, "HTTP 503"):
            self.build()

    def test_group_without_group_id_fails_the_build(self):
        self.sources.responses[f"{job.TCGCSV}/tcgplayer/92/groups"] = (
            b'{"totalItems": 1, "success": true, "errors": [], "results": [{"name": "x"}]}'
        )
        with self.assertRaisesRegex(job.JobError, "groupId"):
            self.build()

    def test_new_tcgcsv_build_during_the_sync_fails_the_build(self):
        answers = iter([LAST_UPDATED, b"2026-11-20T20:03:10+0000"])
        real_get = self.sources.get

        def get(url):
            return next(answers) if url.endswith("last-updated.txt") else real_get(url)

        with self.assertRaisesRegex(job.JobError, "new build during the sync"):
            job.build(self.out, self.previous, get=get, sleep=self.sleeps.append)

    def test_same_tcgcsv_build_reuses_the_published_game_files(self):
        self.build()
        self.publish()
        before = (self.out / "tcgplayer/79.json").read_bytes()
        self.sources.urls.clear()
        self.build()
        self.assertEqual(self.sources.urls, [f"{job.TCGCSV}/last-updated.txt", job.ECB_URL])
        self.assertEqual((self.out / "tcgplayer/79.json").read_bytes(), before)
        self.assertEqual(job.validate(self.out, self.previous), [])

    def test_newer_tcgcsv_build_fetches_again(self):
        self.build()
        self.publish()
        self.sources.responses[f"{job.TCGCSV}/last-updated.txt"] = b"2026-11-20T20:03:10+0000"
        self.sources.urls.clear()
        self.build()
        self.assertEqual(len(self.sources.urls), 9)
        doc = json.loads((self.out / "tcgplayer/92.json").read_bytes())
        self.assertEqual(doc["source"]["lastUpdated"], "2026-11-20T20:03:10Z")

    def test_force_fetches_again(self):
        self.build()
        self.publish()
        self.sources.urls.clear()
        self.build(force=True)
        self.assertEqual(len(self.sources.urls), 9)

    def test_unreadable_last_updated_is_null_and_never_reused(self):
        self.sources.responses[f"{job.TCGCSV}/last-updated.txt"] = b"oops"
        self.build()
        self.publish()
        self.assertIsNone(json.loads((self.out / "tcgplayer/79.json").read_bytes())["source"]["lastUpdated"])
        self.sources.urls.clear()
        self.build()
        self.assertEqual(len(self.sources.urls), 9)


class ValidationTests(TempDirs):
    def setUp(self):
        super().setUp()
        self.build()
        self.publish()

    def problems(self, **options):
        return job.validate(self.out, self.previous, **options)

    def test_no_published_files_is_valid(self):
        for rel in job.published_paths():
            (self.previous / rel).unlink()
        self.assertEqual(self.problems(), [])

    def test_empty_file(self):
        (self.out / "tcgplayer/92.json").write_bytes(b"")
        self.assertEqual(self.problems(), ["tcgplayer/92.json: the file is missing or empty"])

    def test_missing_file(self):
        (self.out / "fx/eur.json").unlink()
        self.assertEqual(self.problems(), ["fx/eur.json: the file is missing or empty"])

    def test_json_that_does_not_parse(self):
        (self.out / "tcgplayer/79.json").write_bytes(b'{"version":1,"groups":[')
        (problem,) = self.problems()
        self.assertTrue(problem.startswith("tcgplayer/79.json: the JSON does not parse"), problem)

    def test_every_bad_file_is_reported(self):
        (self.out / "tcgplayer/79.json").write_bytes(b"")
        (self.out / "fx/eur.json").write_bytes(b"[]")
        self.assertEqual(len(self.problems()), 2)

    def test_game_with_no_groups(self):
        self.edit(self.out, "tcgplayer/92.json", lambda doc: doc.update(groups=[], prices={}))
        self.assertEqual(self.problems(), ["tcgplayer/92.json: there are no groups"])

    def test_game_with_no_price_rows(self):
        self.edit(self.out, "tcgplayer/92.json", lambda doc: doc.update(prices={"24881": []}))
        self.assertIn("tcgplayer/92.json: there are no price rows", self.problems(allow_drop=True))

    def test_game_with_groups_and_prices_that_do_not_match(self):
        self.edit(self.out, "tcgplayer/79.json", lambda doc: doc["prices"].pop("23406"))
        self.assertEqual(self.problems(), ["tcgplayer/79.json: the prices keys are not the same as the groups"])

    def test_game_with_wrong_header(self):
        self.edit(self.out, "tcgplayer/79.json", lambda doc: doc.update(category=92, generatedAt="today", version=2))
        self.assertEqual(
            self.problems(),
            [
                "tcgplayer/79.json: version is not 1",
                "tcgplayer/79.json: generatedAt is not a UTC time",
                "tcgplayer/79.json: category is not 79",
            ],
        )

    def test_price_row_without_product_id(self):
        self.edit(self.out, "tcgplayer/92.json", lambda doc: doc["prices"]["24881"].append({"marketPrice": 1.0}))
        self.assertEqual(self.problems(), ["tcgplayer/92.json: a price row has no integer productId"])

    def test_allowed_group_drop(self):
        self.assertEqual(job.allowed_group_drop(1), 1)
        self.assertEqual(job.allowed_group_drop(15), 1)
        self.assertEqual(job.allowed_group_drop(34), 3)

    def with_published_groups(self, count: int) -> None:
        """The published 79 file gets `count` groups; the new file keeps its 3."""

        def change(doc):
            extra = [{"groupId": 90000 + i, "name": f"Old {i}"} for i in range(count - len(doc["groups"]))]
            doc["groups"] += extra
            doc["prices"].update({str(group["groupId"]): [] for group in extra})

        self.edit(self.previous, "tcgplayer/79.json", change)

    def test_one_group_fewer_than_published_is_valid(self):
        self.with_published_groups(4)
        self.assertEqual(self.problems(), [])

    def test_two_groups_fewer_than_published_fails(self):
        self.with_published_groups(5)
        self.assertEqual(
            self.problems(),
            ["tcgplayer/79.json: 3 groups, but the published file has 5 (a drop of 1 is allowed)"],
        )
        self.assertEqual(self.problems(allow_drop=True), [])

    def test_more_groups_than_published_is_valid(self):
        self.edit(self.previous, "tcgplayer/79.json", lambda doc: (doc["groups"].pop(), doc["prices"].pop("24999")))
        self.assertEqual(self.problems(), [])

    def test_group_that_lost_all_its_prices_fails(self):
        self.edit(self.out, "tcgplayer/79.json", lambda doc: doc["prices"].update({"24812": []}))
        self.edit(self.out, "tcgplayer/79.json", lambda doc: doc["prices"]["23406"].append({"productId": 1}))
        self.assertEqual(
            self.problems(),
            ["tcgplayer/79.json: groups with prices in the published file and none now: 24812"],
        )
        self.assertEqual(self.problems(allow_drop=True), [])

    def test_published_file_that_is_not_readable_is_ignored(self):
        (self.previous / "tcgplayer/79.json").write_bytes(b"<html>")
        (self.previous / "tcgplayer/92.json").write_bytes(b"[]")
        self.assertEqual(self.problems(), [])

    def test_fx_problems(self):
        cases = {
            "base is not EUR": lambda doc: doc.update(base="USD"),
            "date is not a YYYY-MM-DD date": lambda doc: doc.update(date="19 November 2026"),
            "there are no rates": lambda doc: doc.update(rates={}),
            "a rate is not a positive number": lambda doc: doc["rates"].update(SEK=0),
            "a rate is not a positive number ": lambda doc: doc["rates"].update(SEK="11.147"),
            "the USD rate is missing": lambda doc: doc["rates"].pop("USD"),
        }
        good = (self.out / "fx/eur.json").read_bytes()
        for problem, change in cases.items():
            with self.subTest(problem):
                (self.out / "fx/eur.json").write_bytes(good)
                self.edit(self.out, "fx/eur.json", change)
                self.assertEqual(self.problems(), [f"fx/eur.json: {problem.strip()}"])


class CommandLineTests(TempDirs):
    def run_main(self, *command):
        return job.main(["--out", str(self.out), "--previous", str(self.previous), *command])

    def test_validate_exit_codes(self):
        self.build()
        self.assertEqual(self.run_main("validate"), 0)
        (self.out / "tcgplayer/79.json").write_bytes(b"")
        self.assertEqual(self.run_main("validate"), 1)

    def test_failed_build_exits_with_1(self):
        with mock.patch.object(job, "build", side_effect=job.HttpStatusError("https://tcgcsv.com/x", 503)):
            self.assertEqual(self.run_main("build"), 1)


class PreviousTests(TempDirs):
    def test_published_files_are_saved(self):
        job.fetch_previous(self.previous, get=lambda url: url.encode())
        self.assertEqual(
            (self.previous / "tcgplayer/79.json").read_bytes(),
            b"https://facit.github.io/reventi-prices/tcgplayer/79.json",
        )
        self.assertTrue((self.previous / "fx/eur.json").is_file())

    def test_404_means_not_published_and_removes_an_old_copy(self):
        (self.previous / "fx").mkdir(parents=True)
        (self.previous / "fx/eur.json").write_bytes(b"old")
        job.fetch_previous(self.previous, get=self.sources.get)
        self.assertFalse((self.previous / "fx/eur.json").exists())
        self.assertFalse((self.previous / "tcgplayer/79.json").exists())

    def test_other_errors_fail(self):
        def get(url):
            raise job.HttpStatusError(url, 500)

        with self.assertRaises(job.HttpStatusError):
            job.fetch_previous(self.previous, get=get)


class HttpGetTests(unittest.TestCase):
    URL = "https://tcgcsv.com/tcgplayer/79/groups"

    def response(self, body: bytes, encoding=None):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = body
        response.headers.get.side_effect = {"Content-Encoding": encoding}.get
        return response

    def error(self, status: int):
        return urllib.error.HTTPError(self.URL, status, "error", {}, io.BytesIO(b""))

    def get(self, *answers):
        self.sleeps = []
        with mock.patch("urllib.request.urlopen", side_effect=list(answers)) as urlopen:
            try:
                return job.http_get(self.URL, sleep=self.sleeps.append)
            finally:
                self.requests = [call.args[0] for call in urlopen.call_args_list]

    def test_sends_the_user_agent(self):
        self.assertEqual(self.get(self.response(b"{}")), b"{}")
        self.assertEqual(self.requests[0].get_header("User-agent"), job.USER_AGENT)
        self.assertRegex(job.USER_AGENT, r"^reventi-prices/\d+\.\d+\.\d+ \(\+https://github\.com/facit/reventi-prices\)$")

    def test_reads_a_gzip_response(self):
        import gzip

        self.assertEqual(self.get(self.response(gzip.compress(b'{"a":1}'), "gzip")), b'{"a":1}')

    def test_retries_a_server_error(self):
        self.assertEqual(self.get(self.error(503), self.error(429), self.response(b"ok")), b"ok")
        self.assertEqual(self.sleeps, [2, 5])

    def test_retries_a_network_error(self):
        self.assertEqual(self.get(TimeoutError("timed out"), self.response(b"ok")), b"ok")
        self.assertEqual(self.sleeps, [2])

    def test_stops_after_the_last_retry(self):
        with self.assertRaises(job.HttpStatusError) as caught:
            self.get(*[self.error(503)] * 4)
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(self.sleeps, [2, 5, 15])
        self.assertEqual(len(self.requests), 4)

    def test_does_not_retry_a_404(self):
        with self.assertRaises(job.HttpStatusError) as caught:
            self.get(self.error(404))
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(self.sleeps, [])


if __name__ == "__main__":
    unittest.main()
