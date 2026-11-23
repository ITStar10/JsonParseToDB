"""Tests for accounts, approval flow and per-user data access. Run: python -m unittest -v"""
import json
import tempfile
import unittest
from pathlib import Path

from app import PUBLIC_API, USER_POST_API, WRITE_METHODS, create_app
from database import Table, Column
from filters import parse_column_filter, split_terms
from test_browser import ADMIN, login_admin, make_db, settings

USER = {"fullName": "Uma User", "email": "uma@example.com", "password": "user-pass-123"}


class AuthTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = make_db(self.tmp.name, "data.sqlite3")
        self.app = create_app(settings(self.path, users_db=Path(self.tmp.name, "users.sqlite3")))
        self.app.testing = True
        self.users = self.app.extensions["users"]
        self.admin = login_admin(self.app)

    def tearDown(self):
        self.app.extensions["db"].close()
        self.users.close()
        self.tmp.cleanup()

    def signup(self, who=USER):
        return self.app.test_client().post("/api/auth/signup", json=who)

    def login(self, who=USER):
        client = self.app.test_client()
        res = client.post("/api/auth/login", json={"email": who["email"], "password": who["password"]})
        return client, res

    def user_id(self, email=USER["email"]):
        return next(u["id"] for u in self.users.list() if u["email"] == email)

    def approved_user(self, rules=None):
        self.assertEqual(self.signup().status_code, 201)
        uid = self.user_id()
        res = self.admin.put(f"/api/admin/users/{uid}/access", json={"rules": rules or {}, "approve": True})
        self.assertEqual(res.status_code, 200, res.json)
        client, res = self.login()
        self.assertEqual(res.status_code, 200, res.json)
        return uid, client


class SignupFlowTests(AuthTestCase):
    def test_signup_pending_until_approved(self):
        self.assertEqual(self.signup().status_code, 201)
        self.assertEqual(self.users.list("pending")[0]["email"], USER["email"])
        _, res = self.login()
        self.assertEqual((res.status_code, res.json["code"]), (403, "pending"))
        uid = self.user_id()
        self.admin.post(f"/api/admin/users/{uid}/status", json={"status": "approved"})
        client, res = self.login()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(client.get("/api/auth/me").json["user"]["role"], "user")

    def test_signup_validation(self):
        bad = [{**USER, "password": "short"}, {**USER, "email": "nope"}, {**USER, "fullName": "  "},
               {**USER, "fullName": "x" * 500}]
        for body in bad:
            self.assertEqual(self.signup(body).status_code, 400, body)
        self.assertEqual(self.signup().status_code, 201)
        self.assertEqual(self.signup({**USER, "email": USER["email"].upper()}).status_code, 400)  # duplicate

    def test_rejected_and_disabled_cannot_sign_in(self):
        uid, client = self.approved_user()
        self.assertEqual(client.get("/api/tables").status_code, 200)
        self.admin.post(f"/api/admin/users/{uid}/status", json={"status": "disabled"})
        self.assertEqual(client.get("/api/tables").status_code, 401)  # existing session ends now
        self.assertEqual(self.login()[1].json["code"], "disabled")
        self.admin.post(f"/api/admin/users/{uid}/status", json={"status": "rejected"})
        self.assertEqual(self.login()[1].json["code"], "rejected")

    def test_wrong_password_and_unknown_email(self):
        self.approved_user()
        self.assertEqual(self.login({**USER, "password": "wrong-password"})[1].status_code, 401)
        self.assertEqual(self.login({**USER, "email": "ghost@example.com"})[1].status_code, 401)

    def test_login_rate_limit(self):
        codes = [self.login({**USER, "password": "bad-password"})[1].status_code for _ in range(11)]
        self.assertEqual(codes[-1], 429)

    def test_change_password_signs_out_other_sessions(self):
        _, client = self.approved_user()
        other, _ = self.login()
        res = client.post("/api/auth/password", json={"currentPassword": USER["password"], "newPassword": "new-pass-456"})
        self.assertEqual(res.status_code, 200, res.json)
        self.assertEqual(client.get("/api/tables").status_code, 200)
        self.assertEqual(other.get("/api/tables").status_code, 401)
        self.assertEqual(self.login({**USER, "password": "new-pass-456"})[1].status_code, 200)

    def test_logout(self):
        _, client = self.approved_user()
        client.post("/api/auth/logout", json={})
        self.assertEqual(client.get("/api/tables").status_code, 401)

    def test_pages_redirect(self):
        anon = self.app.test_client()
        self.assertEqual(anon.get("/").headers["Location"], "/login")
        self.assertEqual(anon.get("/admin").headers["Location"], "/login")
        self.assertEqual(anon.get("/login").status_code, 200)
        _, client = self.approved_user()
        self.assertEqual(client.get("/admin").headers["Location"], "/")
        self.assertEqual(client.get("/").status_code, 200)
        self.assertEqual(self.admin.get("/admin").status_code, 200)

    def test_cross_origin_write_blocked(self):
        res = self.admin.post("/api/tables/records/delete", json={"keys": [1]},
                              headers={"Origin": "http://evil.example"})
        self.assertEqual(res.status_code, 403)


class ProxySettingsTests(unittest.TestCase):
    def make(self, **extra):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = make_db(tmp.name, "data.sqlite3")
        base = settings(path, users_db=Path(tmp.name, "users.sqlite3"))
        from dataclasses import replace
        app = create_app(replace(base, **extra))
        self.addCleanup(app.extensions["users"].close)
        self.addCleanup(app.extensions["db"].close)
        return app

    def test_public_host_rejected_unless_allowed(self):
        app = self.make()
        self.assertEqual(app.test_client().get("/login", headers={"Host": "data.example.com"}).status_code, 403)
        app = self.make(allowed_hosts=("data.example.com",))
        client = app.test_client()
        self.assertEqual(client.get("/login", headers={"Host": "data.example.com"}).status_code, 200)
        self.assertEqual(client.get("/login", headers={"Host": "evil.example"}).status_code, 403)
        # Proxy that rewrites Host to the upstream: the browser Origin is the public name.
        res = client.post("/api/auth/login", json={"email": "a@b.co", "password": "x" * 8},
                          headers={"Origin": "https://data.example.com"})
        self.assertEqual(res.status_code, 401)  # reached the login check, not blocked
        res = client.post("/api/auth/login", json={"email": "a@b.co", "password": "x" * 8},
                          headers={"Origin": "https://evil.example"})
        self.assertEqual(res.status_code, 403)

    def test_trust_proxy_uses_forwarded_client_ip(self):
        app = self.make(allowed_hosts=("data.example.com",), trust_proxy=True)
        client = app.test_client()
        hdr = lambda ip: {"Host": "data.example.com", "X-Forwarded-For": ip}  # noqa: E731
        body = {"email": "x@example.com", "password": "wrong-pass"}
        codes = [client.post("/api/auth/login", json=body, headers=hdr("10.0.0.1")).status_code for _ in range(11)]
        self.assertEqual(codes[-1], 429)
        # A different client behind the same proxy is not affected.
        self.assertEqual(client.post("/api/auth/login", json=body, headers=hdr("10.0.0.2")).status_code, 401)


class AdminGuardTests(AuthTestCase):
    def admin_id(self):
        return self.user_id(ADMIN["email"])

    def test_cannot_lock_out_last_admin(self):
        aid = self.admin_id()
        for url, body in ((f"/api/admin/users/{aid}/status", {"status": "disabled"}),
                          (f"/api/admin/users/{aid}/role", {"role": "user"})):
            self.assertEqual(self.admin.post(url, json=body).status_code, 400)
        self.assertEqual(self.admin.delete(f"/api/admin/users/{aid}").status_code, 400)

    def test_promote_and_demote(self):
        uid, client = self.approved_user()
        self.admin.post(f"/api/admin/users/{uid}/role", json={"role": "admin"})
        self.assertEqual(client.get("/api/tables").status_code, 401)  # role change re-auth
        client, _ = self.login()
        self.assertEqual(client.get("/api/admin/users").status_code, 200)
        # Now two admins: the second may demote the first.
        aid = self.admin_id()
        self.assertEqual(client.post(f"/api/admin/users/{aid}/role", json={"role": "user"}).status_code, 200)

    def test_admin_reset_password_and_delete(self):
        uid, _ = self.approved_user()
        self.assertEqual(self.admin.post(f"/api/admin/users/{uid}/password", json={"password": "reset-pass-1"}).status_code, 200)
        self.assertEqual(self.login({**USER, "password": "reset-pass-1"})[1].status_code, 200)
        self.assertEqual(self.admin.delete(f"/api/admin/users/{uid}").status_code, 200)
        self.assertEqual(self.login({**USER, "password": "reset-pass-1"})[1].status_code, 401)


class RouteProtectionTests(AuthTestCase):
    def test_every_api_route_requires_auth_and_admin_for_writes(self):
        _, user = self.approved_user()
        anon = self.app.test_client()
        checked = 0
        for rule in self.app.url_map.iter_rules():
            if not rule.rule.startswith("/api/") or rule.rule in PUBLIC_API:
                continue
            url = rule.rule.replace("<path:name>", "records").replace("<int:user_id>", "1").replace("<key>", "1")
            for method in rule.methods - {"HEAD", "OPTIONS"}:
                kwargs = {"json": {}} if method in WRITE_METHODS and method != "DELETE" else {}
                res = anon.open(url, method=method, **kwargs)
                self.assertEqual(res.status_code, 401, f"anonymous {method} {url}")
                user_row_update = method == "PATCH" and rule.endpoint == "update_row"  # column-limited, see below
                admin_only = (rule.rule.startswith("/api/admin/") or rule.rule == "/api/reload"
                              or (method in WRITE_METHODS and rule.rule not in USER_POST_API and not user_row_update))
                if admin_only:
                    res = user.open(url, method=method, **kwargs)
                    self.assertEqual(res.status_code, 403, f"user {method} {url}")
                checked += 1
        self.assertGreater(checked, 15)

    def test_user_status_hides_server_details(self):
        _, user = self.approved_user()
        status = user.get("/api/status").json
        self.assertNotIn("path", status)
        self.assertIn("path", self.admin.get("/api/status").json)

    def test_user_sees_read_only_table(self):
        _, user = self.approved_user()
        table = user.get("/api/tables/records/rows").json["table"]
        self.assertFalse(table["editable"])
        self.assertTrue(self.admin.get("/api/tables/records/rows").json["table"]["editable"])


class DataAccessTests(AuthTestCase):
    """PEOPLE fixture: Alice/Austin, Bob/Boston, Carla/austin, dave/Chicago."""

    def names(self, client, **params):
        if "columnFilters" in params:
            params["columnFilters"] = json.dumps(params["columnFilters"])
        res = client.get("/api/tables/records/rows", query_string=params)
        self.assertEqual(res.status_code, 200, res.json)
        return [r["values"]["firstName"] for r in res.json["rows"]]

    def test_filtered_access_applies_everywhere(self):
        _, user = self.approved_user({"records": {"mode": "filtered", "filters": {"city": "=austin"}}})
        self.assertEqual(self.names(user), ["Alice", "Carla"])
        # The user's own filters narrow further but can never widen the scope.
        self.assertEqual(self.names(user, columnFilters={"city": "boston"}), [])
        self.assertEqual(self.names(user, filters=json.dumps(
            [{"column": "city", "op": "equals", "value": "Boston"}])), [])
        self.assertEqual(self.names(user, search="Bob"), [])
        self.assertEqual(user.get("/api/tables").json["tables"][0]["rows"], 2)
        values = user.get("/api/tables/records/values", query_string={"column": "firstName"}).json["values"]
        self.assertEqual([v["value"] for v in values], ["Alice", "Carla"])
        self.assertTrue(user.get("/api/tables/records/rows").json["table"]["restricted"])
        # Admin still sees everything.
        self.assertEqual(len(self.names(self.admin)), 4)

    def test_multi_value_and_range_rules(self):
        _, user = self.approved_user({"records": {"mode": "filtered", "filters": {
            "city": "boston, chicago", "dateOfBirth": "1980..1989"}}})
        self.assertEqual(self.names(user), ["Bob"])

    def test_no_access_hides_table(self):
        _, user = self.approved_user({"records": {"mode": "none"}})
        self.assertEqual(user.get("/api/tables").json["tables"], [])
        self.assertEqual(user.get("/api/tables/records/rows").status_code, 403)
        self.assertEqual(user.get("/api/tables/records/values", query_string={"column": "city"}).status_code, 403)

    def test_broken_rule_fails_closed(self):
        uid, user = self.approved_user()
        # Simulate a rule left behind after a schema change.
        self.users.set_access_rules(uid, {"records": {"mode": "filtered", "filters": {"gone": "x"}}})
        self.assertEqual(user.get("/api/tables/records/rows").status_code, 403)
        self.assertEqual(user.get("/api/tables").json["tables"], [])

    def test_rule_validation(self):
        self.signup()
        uid = self.user_id()
        url = f"/api/admin/users/{uid}/access"
        bad = [{"records": {"mode": "filtered", "filters": {}}},
               {"records": {"mode": "filtered", "filters": {"city": ">"}}},
               {"records": {"mode": "filtered", "filters": {"nope": "x"}}},
               {"records": {"mode": "maybe"}},
               {"missing_table": {"mode": "all"}}]
        for rules in bad:
            res = self.admin.put(url, json={"rules": rules})
            self.assertIn(res.status_code, (400, 404), rules)
        self.assertEqual(self.users.get(uid)["status"], "pending")

    def test_access_preview(self):
        res = self.admin.post("/api/admin/access-preview", json={"table": "records", "filters": {"city": "austin"}})
        self.assertEqual(res.json, {"matched": 2, "total": 4})

    def test_access_endpoint_lists_schema_and_rules(self):
        uid, _ = self.approved_user({"records": {"mode": "filtered", "filters": {"city": "=austin"}}})
        data = self.admin.get(f"/api/admin/users/{uid}/access").json
        self.assertEqual({k: data["rules"]["records"][k] for k in ("mode", "filters", "sample")},
                         {"mode": "filtered", "filters": {"city": "=austin"}, "sample": None})
        self.assertEqual(data["tables"][0]["name"], "records")
        self.assertIn("dateOfBirth", [c["name"] for c in data["tables"][0]["columns"]])


class DefaultAccessTests(AuthTestCase):
    """Default rules apply to every user without their own rule; a user's rule replaces them."""
    AUSTIN = {"records": {"mode": "filtered", "filters": {"city": "=austin"}}}

    def set_default(self, rules):
        res = self.admin.put("/api/admin/access-defaults", json={"rules": rules})
        self.assertEqual(res.status_code, 200, res.json)

    def names(self, client):
        res = client.get("/api/tables/records/rows")
        self.assertEqual(res.status_code, 200, res.json)
        return [r["values"]["firstName"] for r in res.json["rows"]]

    def test_default_applies_to_rows_counts_and_values(self):
        self.set_default(self.AUSTIN)
        _, user = self.approved_user()  # no rule of their own -> inherits
        self.assertEqual(self.names(user), ["Alice", "Carla"])
        self.assertEqual(user.get("/api/tables").json["tables"][0]["rows"], 2)
        values = user.get("/api/tables/records/values", query_string={"column": "firstName"}).json["values"]
        self.assertEqual([v["value"] for v in values], ["Alice", "Carla"])
        self.assertEqual(len(self.names(self.admin)), 4)  # admins are never restricted

    def test_change_applies_immediately_to_signed_in_user(self):
        _, user = self.approved_user()
        self.assertEqual(len(self.names(user)), 4)
        self.set_default(self.AUSTIN)
        self.assertEqual(self.names(user), ["Alice", "Carla"])
        self.set_default({})
        self.assertEqual(len(self.names(user)), 4)

    def test_user_rule_replaces_default_per_table(self):
        self.set_default(self.AUSTIN)
        uid, user = self.approved_user({"records": {"mode": "all"}})  # explicit all rows
        self.assertEqual(len(self.names(user)), 4)
        self.admin.put(f"/api/admin/users/{uid}/access", json={"rules": {"records": {"mode": "none"}}})
        self.assertEqual(user.get("/api/tables/records/rows").status_code, 403)
        self.admin.put(f"/api/admin/users/{uid}/access", json={"rules": {"records": {"mode": "inherit"}}})
        self.assertEqual(self.names(user), ["Alice", "Carla"])  # back to the default
        self.assertEqual(self.users.access_rules(uid), {})  # "inherit" is never stored

    def test_default_none_hides_table(self):
        self.set_default({"records": {"mode": "none"}})
        _, user = self.approved_user()
        self.assertEqual(user.get("/api/tables").json["tables"], [])
        self.assertEqual(user.get("/api/tables/records/rows").status_code, 403)

    def test_broken_default_fails_closed(self):
        self.users.set_default_rules({"records": {"mode": "filtered", "filters": {"gone": "x"}}})
        _, user = self.approved_user()
        self.assertEqual(user.get("/api/tables/records/rows").status_code, 403)
        self.assertEqual(user.get("/api/tables").json["tables"], [])
        self.assertEqual(user.get("/api/tables/records/values", query_string={"column": "city"}).status_code, 403)

    def test_default_validation(self):
        bad = [{"records": {"mode": "filtered", "filters": {}}},
               {"records": {"mode": "filtered", "filters": {"city": ">"}}},
               {"records": {"mode": "inherit"}},  # the default itself can't inherit
               {"records": {"mode": "maybe"}}, {"nope": {"mode": "all"}}]
        for rules in bad:
            res = self.admin.put("/api/admin/access-defaults", json={"rules": rules})
            self.assertIn(res.status_code, (400, 404), rules)
        self.assertEqual(self.users.default_rules(), {})

    def test_defaults_endpoints(self):
        self.set_default(self.AUSTIN)
        data = self.admin.get("/api/admin/access-defaults").json
        self.assertEqual({k: data["rules"]["records"][k] for k in ("mode", "filters")}, self.AUSTIN["records"])
        self.assertEqual(data["tables"][0]["name"], "records")
        self.signup()
        uid = self.user_id()
        self.assertEqual(self.admin.get(f"/api/admin/users/{uid}/access").json["defaults"]["records"]["filters"],
                         self.AUSTIN["records"]["filters"])

    def test_defaults_are_admin_only(self):
        _, user = self.approved_user()
        self.assertEqual(user.get("/api/admin/access-defaults").status_code, 403)
        self.assertEqual(user.put("/api/admin/access-defaults", json={"rules": {}}).status_code, 403)
        self.assertEqual(self.app.test_client().get("/api/admin/access-defaults").status_code, 401)


class RandomSampleTests(unittest.TestCase):
    """'Show N random rows': per-user, stable, exact size, applied before the user's own filters."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        src = Path(self.tmp.name, "json")
        src.mkdir()
        people = [{"firstName": f"P{n:03d}", "city": "Austin" if n % 2 else "Boston", "id": str(n)}
                  for n in range(200)]
        (src / "p.json").write_text(json.dumps(people), encoding="utf-8")
        self.path = Path(self.tmp.name, "data.sqlite3")
        from dbcreator import import_folder
        import_folder(src, self.path)
        self.app = create_app(settings(self.path, users_db=Path(self.tmp.name, "users.sqlite3")))
        self.app.testing = True
        self.users = self.app.extensions["users"]
        self.admin = login_admin(self.app)

    def tearDown(self):
        self.app.extensions["db"].close()
        self.users.close()
        self.tmp.cleanup()

    def add_user(self, n):
        u = self.users.create(f"User {n}", f"u{n}@example.com", "user-pass-123", status="approved")
        client = self.app.test_client()
        self.assertEqual(client.post("/api/auth/login", json={"email": f"u{n}@example.com",
                                                              "password": "user-pass-123"}).status_code, 200)
        return u["id"], client

    def publish(self, rule, **kw):
        res = self.admin.put("/api/admin/access-defaults", json={"rules": {"records": rule}})
        self.assertEqual(res.status_code, 200, res.json)

    def ids(self, client, **params):
        out, page = [], 1
        while True:
            r = client.get("/api/tables/records/rows", query_string={"page": page, "pageSize": 50, **params}).json
            out += [x["values"]["_id"] for x in r["rows"]]
            if page >= r["pages"]:
                return out, r
            page += 1

    def test_exact_size_within_filter_and_stable_across_pages_and_requests(self):
        self.publish({"mode": "filtered", "filters": {"city": "=austin"}, "sample": 30})  # 100 match
        _, user = self.add_user(1)
        first, meta = self.ids(user)
        self.assertEqual((len(first), len(set(first)), meta["total"]), (30, 30, 30))
        self.assertEqual(meta["table"]["sample"], 30)
        self.assertEqual(self.ids(user)[0], first)  # same rows on every visit and across pages
        for row_id in first:  # only rows that match the access filter
            self.assertEqual(self.admin.get("/api/tables/records/rows", query_string={
                "columnFilters": json.dumps({"_id": f"={row_id}"})}).json["rows"][0]["values"]["city"], "Austin")
        self.assertEqual(user.get("/api/tables").json["tables"][0]["rows"], 30)

    def test_each_user_gets_their_own_sample(self):
        self.publish({"mode": "all", "sample": 20})
        samples = [frozenset(self.ids(self.add_user(n)[1])[0]) for n in range(1, 6)]
        self.assertTrue(all(len(x) == 20 for x in samples))
        self.assertGreater(len(set(samples)), 1)  # not everyone gets the same rows
        self.assertGreater(len(frozenset().union(*samples)), 20)

    def test_user_filters_apply_inside_the_sample_only(self):
        self.publish({"mode": "all", "sample": 25})
        _, user = self.add_user(1)
        sample = set(self.ids(user)[0])
        austin, meta = self.ids(user, columnFilters=json.dumps({"city": "=austin"}))
        self.assertTrue(set(austin) <= sample)
        self.assertEqual(meta["total"], len(austin))
        values = user.get("/api/tables/records/values", query_string={"column": "_id"}).json["values"]
        self.assertEqual({v["value"] for v in values}, sample)  # checklists only offer sampled rows

    def test_admin_sees_everything(self):
        self.publish({"mode": "all", "sample": 5})
        self.assertEqual(self.admin.get("/api/tables/records/rows").json["total"], 200)

    def test_size_change_keeps_picks_and_reshuffle_changes_them(self):
        self.publish({"mode": "all", "sample": 10})
        _, user = self.add_user(1)
        ten = set(self.ids(user)[0])
        self.publish({"mode": "all", "sample": 25})  # same seed: a superset, no churn
        twenty_five = set(self.ids(user)[0])
        self.assertEqual((len(twenty_five), ten <= twenty_five), (25, True))
        self.publish({"mode": "all", "sample": 25})  # saving again changes nothing
        self.assertEqual(set(self.ids(user)[0]), twenty_five)
        res = self.admin.put("/api/admin/access-defaults", json={"rules": {
            "records": {"mode": "all", "sample": 25, "reshuffle": True}}})
        self.assertEqual(res.status_code, 200)
        self.assertNotEqual(set(self.ids(user)[0]), twenty_five)
        self.publish({"mode": "all"})  # sample removed
        self.assertEqual(len(self.ids(user)[0]), 200)

    def test_per_user_override_has_its_own_sample(self):
        self.publish({"mode": "all", "sample": 10})
        uid, user = self.add_user(1)
        self.admin.put(f"/api/admin/users/{uid}/access", json={"rules": {"records": {"mode": "all", "sample": 40}}})
        self.assertEqual(len(self.ids(user)[0]), 40)
        self.admin.put(f"/api/admin/users/{uid}/access", json={"rules": {"records": {"mode": "inherit"}}})
        self.assertEqual(len(self.ids(user)[0]), 10)

    def test_sample_larger_than_matches_shows_all_matches(self):
        self.publish({"mode": "filtered", "filters": {"city": "=austin"}, "sample": 5000})
        self.assertEqual(len(self.ids(self.add_user(1)[1])[0]), 100)

    def test_validation(self):
        for bad in (-3, 2.5, "ten", True, 10 ** 9):
            res = self.admin.put("/api/admin/access-defaults", json={"rules": {"records": {"mode": "all", "sample": bad}}})
            self.assertEqual(res.status_code, 400, bad)
        self.assertEqual(self.users.default_rules(), {})

    def test_table_without_row_key_cannot_be_sampled(self):
        import sqlite3
        with sqlite3.connect(self.path) as conn:  # composite key + WITHOUT ROWID: no single row identity
            conn.executescript("""CREATE TABLE pairs (a TEXT, b TEXT, PRIMARY KEY (a, b)) WITHOUT ROWID;
                                  INSERT INTO pairs VALUES ('x','1'), ('x','2'), ('y','1');""")
        res = self.admin.put("/api/admin/access-defaults", json={"rules": {"pairs": {"mode": "all", "sample": 2}}})
        self.assertEqual(res.status_code, 400)
        self.assertIn("no row key", res.json["error"])
        # A rule that slipped in anyway (e.g. edited in the users DB) denies access instead of showing everything.
        self.users.set_default_rules({"pairs": {"mode": "all", "sample": 2, "seed": 5}})
        _, user = self.add_user(1)
        self.assertEqual(user.get("/api/tables/pairs/rows").status_code, 403)
        self.assertEqual([t["name"] for t in user.get("/api/tables").json["tables"]], ["records"])

    def test_rank_is_stable_and_seed_dependent(self):
        from database import sample_rank
        self.assertEqual(sample_rank(7, "a:1"), sample_rank(7, "a:1"))
        order = lambda seed: sorted(range(100), key=lambda k: sample_rank(k, seed))  # noqa: E731
        self.assertNotEqual(order("a:1"), order("a:2"))
        self.assertTrue(all(0 <= sample_rank(k, "x") < 2 ** 63 for k in range(50)))

    def test_preview_unchanged_and_defaults_endpoint_reports_sample(self):
        self.publish({"mode": "all", "sample": 12})
        rule = self.admin.get("/api/admin/access-defaults").json["rules"]["records"]
        self.assertEqual(rule["sample"], 12)
        self.assertTrue(rule["seed"])


class UsersDatabaseUpgradeTests(unittest.TestCase):
    def test_old_users_database_gets_sample_columns(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "old.sqlite3")
            conn = sqlite3.connect(path)
            conn.executescript("""
                CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, full_name TEXT NOT NULL,
                    email TEXT NOT NULL UNIQUE COLLATE NOCASE, password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'user', status TEXT NOT NULL DEFAULT 'pending',
                    session_version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
                    decided_at TEXT, decided_by INTEGER, last_login_at TEXT);
                CREATE TABLE default_access_rules (table_name TEXT PRIMARY KEY, mode TEXT NOT NULL,
                    filters TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL);
                CREATE TABLE access_rules (user_id INTEGER NOT NULL, table_name TEXT NOT NULL, mode TEXT NOT NULL,
                    filters TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL, PRIMARY KEY (user_id, table_name));
                INSERT INTO default_access_rules VALUES ('records', 'filtered', '{"city": "=x"}', '2026-01-01');
            """)
            conn.commit()
            conn.close()
            from auth import UserStore
            store = UserStore(path)
            try:
                rule = store.default_rules()["records"]
                self.assertEqual((rule["mode"], rule["filters"], rule["sample"], rule["seed"]),
                                 ("filtered", {"city": "=x"}, None, 0))
                store.set_default_rules({"records": {"mode": "all", "sample": 7, "seed": 99}})
                self.assertEqual(store.default_rules()["records"]["sample"], 7)
            finally:
                store.close()


class EditPermissionTests(unittest.TestCase):
    """Regular users may update admin-approved columns (default: curUser) in rows they can see.
    They can never add or delete rows."""

    def setUp(self):
        import sqlite3
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name, "data.sqlite3")
        with sqlite3.connect(self.path) as conn:
            conn.executescript("""
                CREATE TABLE jobs (id INTEGER PRIMARY KEY, title TEXT, city TEXT, curUser TEXT, notes TEXT, pic BLOB);
                INSERT INTO jobs (title, city, curUser) VALUES ('a','Austin',NULL), ('b','Boston',NULL),
                    ('c','Austin','old'), ('d','Denver',NULL);
                CREATE TABLE plain (id INTEGER PRIMARY KEY, name TEXT);
                INSERT INTO plain (name) VALUES ('x'), ('y');
            """)
        self.app = create_app(settings(self.path, users_db=Path(self.tmp.name, "users.sqlite3")))
        self.app.testing = True
        self.users = self.app.extensions["users"]
        self.admin = login_admin(self.app)
        self.users.create("Uma User", "uma@example.com", "user-pass-123", status="approved")
        self.user = self.app.test_client()
        self.assertEqual(self.user.post("/api/auth/login", json={"email": "uma@example.com",
                                                                 "password": "user-pass-123"}).status_code, 200)

    def tearDown(self):
        self.app.extensions["db"].close()
        self.users.close()
        self.tmp.cleanup()

    def patch(self, client, table, key, values):
        return client.patch(f"/api/tables/{table}/rows/{key}", json={"values": values})

    def title(self, key):
        return self.admin.get("/api/tables/jobs/rows", query_string={
            "columnFilters": json.dumps({"id": f"={key}"})}).json["rows"][0]["values"]

    def set_editable(self, rules):
        res = self.admin.put("/api/admin/editable-columns", json={"rules": rules})
        self.assertEqual(res.status_code, 200, res.json)
        return res.json

    def test_default_user_can_edit_only_curuser(self):
        self.assertEqual(self.user.get("/api/tables/jobs/rows").json["table"]["editableColumns"], ["curUser"])
        res = self.patch(self.user, "jobs", 1, {"curUser": "Uma"})
        self.assertEqual((res.status_code, res.json["row"]["values"]["curUser"]), (200, "Uma"))
        self.assertEqual(self.title(1)["curUser"], "Uma")
        for values in ({"title": "hacked"}, {"curUser": "Uma", "title": "x"}, {"id": 99}, {"nope": 1}):
            res = self.patch(self.user, "jobs", 2, values)
            self.assertEqual(res.status_code, 403, values)
        self.assertEqual(self.title(2)["title"], "b")  # nothing was partially applied

    def test_tables_without_curuser_are_not_editable_by_default(self):
        self.assertEqual(self.user.get("/api/tables/plain/rows").json["table"]["editableColumns"], [])
        res = self.patch(self.user, "plain", 1, {"name": "z"})
        self.assertEqual(res.status_code, 403)
        self.assertIn("hasn't allowed", res.json["error"])

    def test_users_can_never_add_or_delete(self):
        self.assertEqual(self.user.delete("/api/tables/jobs/rows/1").status_code, 403)
        self.assertEqual(self.user.post("/api/tables/jobs/delete", json={"keys": [1]}).status_code, 403)
        self.assertEqual(self.user.post("/api/tables/jobs/rows", json={"values": {"title": "n"}}).status_code, 403)
        self.set_editable({"jobs": ["curUser", "title", "city"]})  # even with every column editable
        self.assertEqual(self.user.delete("/api/tables/jobs/rows/1").status_code, 403)
        self.assertEqual(self.admin.get("/api/tables/jobs/rows").json["total"], 4)

    def test_only_rows_inside_the_users_access(self):
        self.admin.put("/api/admin/access-defaults", json={"rules": {"jobs": {
            "mode": "filtered", "filters": {"city": "=austin"}}}})
        self.assertEqual(self.patch(self.user, "jobs", 1, {"curUser": "U"}).status_code, 200)  # Austin
        self.assertEqual(self.patch(self.user, "jobs", 2, {"curUser": "U"}).status_code, 404)  # Boston: hidden
        self.assertIsNone(self.title(2)["curUser"])
        self.admin.put("/api/admin/access-defaults", json={"rules": {"jobs": {"mode": "none"}}})
        self.assertEqual(self.patch(self.user, "jobs", 1, {"curUser": "V"}).status_code, 403)

    def test_only_rows_inside_the_random_sample(self):
        self.admin.put("/api/admin/access-defaults", json={"rules": {"jobs": {"mode": "all", "sample": 2}}})
        seen = {r["key"] for r in self.user.get("/api/tables/jobs/rows").json["rows"]}
        self.assertEqual(len(seen), 2)
        for key in (1, 2, 3, 4):
            res = self.patch(self.user, "jobs", key, {"curUser": "S"})
            self.assertEqual(res.status_code, 200 if key in seen else 404, key)

    def test_admin_chooses_editable_columns(self):
        data = self.set_editable({"jobs": ["title", "notes"], "plain": ["name"]})
        jobs = next(t for t in data["tables"] if t["name"] == "jobs")
        self.assertEqual((jobs["editable"], jobs["isDefault"]), (["title", "notes"], False))
        self.assertEqual(self.patch(self.user, "jobs", 1, {"title": "new", "notes": "n"}).status_code, 200)
        self.assertEqual(self.patch(self.user, "jobs", 1, {"curUser": "x"}).status_code, 403)  # no longer allowed
        self.assertEqual(self.patch(self.user, "plain", 1, {"name": "q"}).status_code, 200)
        self.set_editable({"jobs": [], "plain": None})  # none at all / back to default
        self.assertEqual(self.patch(self.user, "jobs", 1, {"title": "z"}).status_code, 403)
        self.assertEqual(self.patch(self.user, "jobs", 1, {"curUser": "x"}).status_code, 403)
        self.set_editable({"jobs": None})
        self.assertEqual(self.patch(self.user, "jobs", 1, {"curUser": "x"}).status_code, 200)

    def test_candidates_exclude_keys_and_blobs_and_validation(self):
        jobs = next(t for t in self.admin.get("/api/admin/editable-columns").json["tables"] if t["name"] == "jobs")
        self.assertEqual([c["name"] for c in jobs["candidates"]], ["title", "city", "curUser", "notes"])
        for rules in ({"jobs": ["id"]}, {"jobs": ["pic"]}, {"jobs": ["ghost"]}, {"jobs": "title"},
                      {"jobs": [1]}, {"nope": ["x"]}):
            res = self.admin.put("/api/admin/editable-columns", json={"rules": rules})
            self.assertIn(res.status_code, (400, 404), rules)
        self.assertEqual(self.users.editable_columns(), {})

    def test_changes_apply_immediately_and_are_admin_only(self):
        self.assertEqual(self.user.get("/api/admin/editable-columns").status_code, 403)
        self.assertEqual(self.user.put("/api/admin/editable-columns", json={"rules": {}}).status_code, 403)
        self.assertEqual(self.app.test_client().get("/api/admin/editable-columns").status_code, 401)
        self.set_editable({"jobs": ["title"]})
        self.assertEqual(self.user.get("/api/tables/jobs/rows").json["table"]["editableColumns"], ["title"])

    def test_read_only_mode_blocks_users(self):
        app = create_app(settings(self.path, read_only=True, users_db=Path(self.tmp.name, "users.sqlite3")))
        client = app.test_client()
        client.post("/api/auth/login", json={"email": "uma@example.com", "password": "user-pass-123"})
        self.assertEqual(client.get("/api/tables/jobs/rows").json["table"]["editableColumns"], [])
        self.assertEqual(self.patch(client, "jobs", 1, {"curUser": "x"}).status_code, 403)
        app.extensions["db"].close()

    def test_validation_errors_still_apply_to_allowed_columns(self):
        self.set_editable({"jobs": ["title"]})
        self.assertEqual(self.patch(self.user, "jobs", 1, "nope").status_code, 400)
        self.assertEqual(self.patch(self.user, "jobs", 999, {"title": "x"}).status_code, 404)
        self.assertEqual(self.patch(self.user, "jobs", "abc", {"title": "x"}).status_code, 400)


class VisibleColumnsTests(unittest.TestCase):
    """Admins choose which columns regular users see; hidden columns must not leak through
    values, filters, sorting, search, value lists or edits."""
    setUp = EditPermissionTests.setUp
    tearDown = EditPermissionTests.tearDown
    patch = EditPermissionTests.patch

    def set_visible(self, rules, status=200):
        res = self.admin.put("/api/admin/visible-columns", json={"rules": rules})
        self.assertEqual(res.status_code, status, res.json)
        return res.json

    def get(self, **params):
        for k in ("columnFilters", "filters"):
            if k in params:
                params[k] = json.dumps(params[k])
        return self.user.get("/api/tables/jobs/rows", query_string=params)

    def test_all_columns_visible_by_default(self):
        cols = [c["name"] for c in self.get().json["table"]["columns"]]
        self.assertEqual(cols, ["id", "title", "city", "curUser", "notes", "pic"])

    def test_hidden_columns_are_not_sent(self):
        self.set_visible({"jobs": ["title", "city", "curUser"]})
        data = self.get().json
        self.assertEqual([c["name"] for c in data["table"]["columns"]], ["title", "city", "curUser"])
        self.assertEqual(data["table"]["hiddenColumns"], 3)
        for row in data["rows"]:
            self.assertEqual(set(row["values"]), {"title", "city", "curUser"})
        self.assertEqual(len(self.admin.get("/api/tables/jobs/rows").json["table"]["columns"]), 6)

    def test_hidden_columns_cannot_be_used_to_probe(self):
        import sqlite3
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE jobs SET notes = 'SECRET-123' WHERE id = 3")
        self.set_visible({"jobs": ["title", "city"]})
        probes = [dict(columnFilters={"notes": "SECRET"}),
                  dict(filters=[{"any": [{"column": "notes", "op": "contains", "value": "SECRET"}]}]),
                  dict(sort="notes"), dict(search="x", searchColumn="notes")]
        for params in probes:
            res = self.get(**params)
            self.assertEqual(res.status_code, 400, params)
            self.assertIn("Unknown column", res.json["error"])  # same as a column that doesn't exist
        self.assertEqual(self.get(search="SECRET").json["total"], 0)  # global search skips hidden columns
        self.assertEqual(self.get(search="Austin").json["total"], 2)
        res = self.user.get("/api/tables/jobs/values", query_string={"column": "notes"})
        self.assertEqual(res.status_code, 400)
        res = self.user.get("/api/tables/jobs/values", query_string={
            "column": "city", "columnFilters": json.dumps({"notes": "SECRET"})})
        self.assertEqual(res.status_code, 400)

    def test_hidden_columns_are_not_editable_and_patch_hides_them(self):
        self.set_visible({"jobs": ["title", "city"]})  # curUser (editable by default) now hidden
        self.assertEqual(self.get().json["table"]["editableColumns"], [])
        self.assertEqual(self.patch(self.user, "jobs", 1, {"curUser": "x"}).status_code, 403)
        self.set_visible({"jobs": ["title", "curUser"]})
        res = self.patch(self.user, "jobs", 1, {"curUser": "Uma"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(set(res.json["row"]["values"]), {"title", "curUser"})

    def test_admin_view_of_editable_columns_matches_what_users_can_do(self):
        self.set_visible({"jobs": ["title", "city"]})  # curUser (editable by default) hidden
        jobs = next(t for t in self.admin.get("/api/admin/editable-columns").json["tables"] if t["name"] == "jobs")
        self.assertEqual(jobs["editable"], [])  # not ["curUser"]: users can't edit a hidden column
        self.assertEqual({c["name"]: c["hidden"] for c in jobs["candidates"]},
                         {"title": False, "city": False, "curUser": True, "notes": True})
        self.assertEqual(self.patch(self.user, "jobs", 1, {"curUser": "x"}).status_code, 403)
        self.set_visible({"jobs": ["title", "curUser"]})
        jobs = next(t for t in self.admin.get("/api/admin/editable-columns").json["tables"] if t["name"] == "jobs")
        self.assertEqual(jobs["editable"], ["curUser"])
        self.assertEqual(self.patch(self.user, "jobs", 1, {"curUser": "x"}).status_code, 200)

    def test_access_rules_may_use_hidden_columns(self):
        self.admin.put("/api/admin/access-defaults", json={"rules": {"jobs": {
            "mode": "filtered", "filters": {"city": "=austin"}}}})
        self.set_visible({"jobs": ["title"]})
        data = self.get().json
        self.assertEqual(sorted(r["values"]["title"] for r in data["rows"]), ["a", "c"])

    def test_validation_and_reset(self):
        for rules in ({"jobs": []}, {"jobs": ["ghost"]}, {"jobs": "title"}, {"jobs": [1]}, {"nope": ["x"]}):
            res = self.admin.put("/api/admin/visible-columns", json={"rules": rules})
            self.assertIn(res.status_code, (400, 404), rules)
        self.set_visible({"jobs": ["title"]})
        self.set_visible({"jobs": None})
        self.assertEqual(len(self.get().json["table"]["columns"]), 6)
        jobs = next(t for t in self.admin.get("/api/admin/visible-columns").json["tables"] if t["name"] == "jobs")
        self.assertTrue(jobs["isDefault"])

    def test_stale_setting_fails_closed(self):
        self.users.set_visible_columns({"jobs": ["renamed_away"]})
        self.assertEqual(self.get().status_code, 403)
        self.assertNotIn("jobs", [t["name"] for t in self.user.get("/api/tables").json["tables"]])

    def test_admin_only(self):
        self.assertEqual(self.user.get("/api/admin/visible-columns").status_code, 403)
        self.assertEqual(self.user.put("/api/admin/visible-columns", json={"rules": {}}).status_code, 403)

    def test_hidden_text_primary_key_is_not_sent_as_row_key(self):
        import sqlite3
        with sqlite3.connect(self.path) as conn:
            conn.executescript("""CREATE TABLE codes (code TEXT PRIMARY KEY, label TEXT) WITHOUT ROWID;
                                  INSERT INTO codes VALUES ('SECRET-KEY', 'one');""")
        self.set_visible({"codes": ["label"]})
        row = self.user.get("/api/tables/codes/rows").json["rows"][0]
        self.assertEqual((row["key"], row["values"]), (None, {"label": "one"}))


class FilterSyntaxTests(unittest.TestCase):
    text = Column("city", "TEXT", "text", False, False, None)
    dob = Column("dob", "TEXT", "date", False, False, None)
    flag = Column("sold", "BOOLEAN", "boolean", False, False, None)

    def test_split_terms_respects_quotes(self):
        self.assertEqual(split_terms('a, "b, c", ="x ""y"""'), ["a", '"b, c"', '="x ""y"""'])

    def test_positive_terms_or_negative_terms_and(self):
        self.assertEqual(parse_column_filter(self.text, "a, =b"), {"any": [
            {"column": "city", "op": "contains", "value": "a"}, {"column": "city", "op": "equals", "value": "b"}]})
        self.assertEqual(parse_column_filter(self.text, "!a, !=b"), {"all": [
            {"column": "city", "op": "not_contains", "value": "a"}, {"column": "city", "op": "not_equals", "value": "b"}]})
        self.assertEqual(parse_column_filter(self.text, '="Main St, 2"'),
                         {"column": "city", "op": "equals", "value": "Main St, 2"})
        self.assertEqual(parse_column_filter(self.text, "="), {"column": "city", "op": "is_empty"})
        self.assertIsNone(parse_column_filter(self.text, ">"))

    def test_date_ranges(self):
        self.assertEqual(parse_column_filter(self.dob, "1980..1989"), {"all": [
            {"column": "dob", "op": "gte", "value": "1980"}, {"column": "dob", "op": "lt", "value": "1990"}]})
        self.assertEqual(parse_column_filter(self.dob, "..1975-12"),
                         {"column": "dob", "op": "lt", "value": "1976-01"})
        self.assertEqual(parse_column_filter(self.dob, "..2000-02-29"),
                         {"column": "dob", "op": "lt", "value": "2000-03-01"})
        self.assertEqual(parse_column_filter(self.text, "a..m"), {"all": [
            {"column": "city", "op": "gte", "value": "a"}, {"column": "city", "op": "lte", "value": "m"}]})

    def test_boolean(self):
        self.assertEqual(parse_column_filter(self.flag, "null"), {"column": "sold", "op": "is_null"})
        self.assertEqual(parse_column_filter(self.flag, "1"), {"column": "sold", "op": "equals", "value": "1"})
        with self.assertRaises(ValueError):
            parse_column_filter(self.flag, "yes")


class StaticAssetTests(unittest.TestCase):
    def test_icon_sprite_is_valid_and_complete(self):
        import re
        import xml.etree.ElementTree as ET
        static = Path(__file__).resolve().parent / "static"
        root = ET.parse(static / "icons.svg").getroot()  # raises on malformed XML
        ids = {el.get("id") for el in root.iter() if el.get("id")}
        used = set()
        for f in static.iterdir():
            if f.suffix in (".html", ".js"):
                text = f.read_text(encoding="utf-8")
                used |= {"i-" + m for m in re.findall(r'icons\.svg#i-([a-z-]+)', text)}
                used |= {"i-" + m for m in re.findall(r'\bicon\("([a-z-]+)"', text)}
        self.assertFalse(used - ids, f"missing icons: {sorted(used - ids)}")


assert Table  # imported for type completeness


if __name__ == "__main__":
    unittest.main()
