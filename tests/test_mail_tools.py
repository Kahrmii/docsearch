"""Tests for the mail filters and statistics of the agent tools (fake Elasticsearch)."""
import json
import unittest

from docsearch import tools


class RecordingEs:
    def __init__(self, hits=None, buckets=None):
        self.bodies, self.hits, self.buckets = [], hits or [], buckets or []

    def search(self, index, body):
        self.bodies.append(body)
        return {"hits": {"total": {"value": len(self.hits)}, "hits": self.hits},
                "aggregations": {"g": {"buckets": self.buckets}}}

    def count(self, index, query):
        self.bodies.append(query)
        return 3


def filters(query):
    return query["bool"]["filter"]


class QueryFilterTest(unittest.TestCase):
    def test_sender_is_a_case_insensitive_contains_on_the_address(self):
        (f,) = filters(tools.build_query(sender="Anna@Example"))
        self.assertEqual(f, {"wildcard": {"mail_from": {"value": "*anna@example*", "case_insensitive": True}}})

    def test_wildcard_characters_in_the_value_are_taken_literally(self):
        (f,) = filters(tools.build_query(subject="50% * off?"))
        self.assertEqual(f["wildcard"]["mail_subject"]["value"], "*50% \\* off\\?*")

    def test_recipient_matches_to_or_cc(self):
        (f,) = filters(tools.build_query(recipient="bernd"))
        fields = [list(clause["wildcard"])[0] for clause in f["bool"]["should"]]
        self.assertEqual(fields, ["mail_to", "mail_cc"])
        self.assertEqual(f["bool"]["minimum_should_match"], 1)

    def test_partial_dates_cover_the_whole_period(self):
        (f,) = filters(tools.build_query(date_from="2021-05", date_to="2021-05-31"))
        self.assertEqual(f["range"]["mail_date"], {"gte": "2021-05||/M", "lte": "2021-05-31||/d"})
        (f,) = filters(tools.build_query(date_from="2020", date_to="2021"))
        self.assertEqual(f["range"]["mail_date"], {"gte": "2020||/y", "lte": "2021||/y"})
        (f,) = filters(tools.build_query(date_to="2021-12-24T10:00:00"))  # anything else is passed on unchanged
        self.assertEqual(f["range"]["mail_date"], {"lte": "2021-12-24T10:00:00"})

    def test_no_filters_keep_the_old_queries(self):
        self.assertEqual(tools.build_query(), {"match_all": {}})
        self.assertEqual(tools.build_query("x", extension="pdf")["bool"]["filter"], [{"term": {"extension": "pdf"}}])


class ToolTest(unittest.TestCase):
    def test_search_shows_mail_fields_only_for_mails(self):
        hits = [{"_source": {"filename": "box.mbox#1", "path": "C:/box.mbox", "content": "Subject: Hi\n\nbody text",
                             "mail_subject": "Hi", "mail_from": "a@b.org", "mail_date": "2021-05-03T08:00:00+00:00",
                             "mail_to": [f"u{i}@x.org" for i in range(9)]}},
                {"_source": {"filename": "report.pdf", "path": "C:/report.pdf", "content": "body text"}}]
        ctx = tools.Context(RecordingEs(hits), "docs", 12000)
        result = tools.search_documents(ctx, query="body", sender="a@b.org")
        mail, normal = result["hits"]
        self.assertEqual((mail["subject"], mail["from"], mail["date"]), ("Hi", "a@b.org", "2021-05-03T08:00:00+00:00"))
        self.assertEqual(len(mail["to"]), 5)  # capped
        self.assertNotIn("subject", normal)
        self.assertEqual(ctx.es.bodies[0]["query"]["bool"]["filter"][0]["wildcard"]["mail_from"]["value"], "*a@b.org*")

    def test_count_documents_accepts_mail_filters(self):
        es = RecordingEs()
        self.assertEqual(tools.count_documents(tools.Context(es, "d", 12000), sender="x", date_from="2021"),
                         {"documents": 3})
        self.assertEqual(len(filters(es.bodies[0])), 2)

    def test_group_by_mail_month_and_sender(self):
        es = RecordingEs(buckets=[{"key": 1, "key_as_string": "2021-05", "doc_count": 4},
                                  {"key": 2, "key_as_string": "2021-06", "doc_count": 0},
                                  {"key": 3, "key_as_string": "2021-07", "doc_count": 1}])
        result = tools.group_by(tools.Context(es, "d", 12000), "mail_month", sender="anna")
        self.assertEqual(result["groups"], [{"value": "2021-05", "documents": 4}, {"value": "2021-07", "documents": 1}])
        agg = es.bodies[0]["aggs"]["g"]["date_histogram"]
        self.assertEqual(agg, {"field": "mail_date", "calendar_interval": "month", "format": "yyyy-MM"})
        tools.group_by(tools.Context(es, "d", 12000), "mail_from", size=5)
        self.assertEqual(es.bodies[1]["aggs"]["g"], {"terms": {"field": "mail_from", "size": 5}})

    def test_the_model_is_told_about_the_mail_filters(self):
        schemas = {t["function"]["name"]: t["function"]["parameters"]["properties"] for t in tools.TOOLS}
        for name in ("search_documents", "count_documents", "group_by"):
            self.assertTrue({"sender", "recipient", "subject", "date_from", "date_to"} <= set(schemas[name]), name)
        group_field = [t for t in tools.TOOLS if t["function"]["name"] == "group_by"][0]["function"]["parameters"]
        self.assertTrue({"mail_from", "mail_to", "mail_month"} <= set(group_field["properties"]["field"]["enum"]))

    def test_unknown_filter_arguments_are_reported_not_crashing(self):
        ctx = tools.Context(RecordingEs(), "d", 12000)
        self.assertIn("invalid arguments", tools.run_tool(ctx, "find_files", {"sender": "x", "name": "y"}))
        self.assertIn("documents", json.loads(tools.run_tool(ctx, "count_documents", {"sender": "x"})))


if __name__ == "__main__":
    unittest.main()
