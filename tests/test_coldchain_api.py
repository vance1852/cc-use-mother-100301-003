import unittest
from datetime import datetime, timezone

from polar_station_foundation.coldchain.api import route_coldchain, route_combined
from polar_station_foundation.coldchain.schema import COLDCHAIN_SCHEMA
from polar_station_foundation.coldchain.service import ColdChainService
from polar_station_foundation.clock import FixedClock
from polar_station_foundation.service import DomainService
from polar_station_foundation.storage import Database

from tests.test_coldchain import plan_config, series


class ColdChainApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database(extra_schema=COLDCHAIN_SCHEMA)
        clock = FixedClock(datetime(2026, 9, 19, 6, 0, tzinfo=timezone.utc))
        self.foundation = DomainService(self.database, clock)
        self.service = ColdChainService(self.database, clock)
        self.foundation.register_organization(request_id="org", actor_id="bootstrap",
                                              organization_id="o1", name="科考机构")
        self.foundation.register_actor(request_id="admin", actor_id="bootstrap",
                                       new_actor_id="a1", display_name="管理员",
                                       role="admin", organization_id="o1")
        self.foundation.register_actor(request_id="operator", actor_id="a1",
                                       new_actor_id="op1", display_name="操作员",
                                       role="operator", organization_id="o1")
        self.foundation.register_actor(request_id="quality", actor_id="a1",
                                       new_actor_id="qa1", display_name="质量负责人",
                                       role="quality", organization_id="o1")
        self.foundation.register_site(request_id="site", actor_id="op1", site_id="s1",
                                      organization_id="o1", name="科考站",
                                      timezone_name="Asia/Shanghai")

    def tearDown(self):
        self.database.close()

    def test_plan_roundtrip_and_replay_status(self):
        body = {"request_id": "api-plan", "plan_id": "plan-api", "site_id": "s1",
                "config": plan_config()}
        status, payload = route_coldchain(self.service, "POST", "/coldchain/plans", body,
                                          {"X-Actor-Id": "op1"})
        self.assertEqual(201, status)
        self.assertFalse(payload["replayed"])
        self.assertEqual("cc_plan", payload["resource_type"])
        status, payload = route_coldchain(self.service, "POST", "/coldchain/plans", body,
                                          {"X-Actor-Id": "op1"})
        self.assertEqual(200, status)
        self.assertTrue(payload["replayed"])
        status, payload = route_coldchain(self.service, "GET",
                                          "/coldchain/plans?plan_id=plan-api", None)
        self.assertEqual(200, status)
        self.assertEqual("plan-api", payload["plan_id"])
        self.assertEqual("locked", payload["status"])

    def test_readings_and_disposition_record_routes(self):
        route_coldchain(self.service, "POST", "/coldchain/plans",
                        {"request_id": "api-plan", "plan_id": "plan-api", "site_id": "s1",
                         "config": plan_config()}, {"X-Actor-Id": "op1"})
        status, _ = route_coldchain(
            self.service, "POST", "/coldchain/readings",
            {"request_id": "api-read", "plan_id": "plan-api", "package_id": "P1",
             "logger_id": "L2", "readings": series(0, 600, 10, -78.0)},
            {"X-Actor-Id": "op1"})
        self.assertEqual(201, status)
        status, _ = route_coldchain(self.service, "POST", "/coldchain/assessments",
                                    {"request_id": "api-assess", "plan_id": "plan-api"},
                                    {"X-Actor-Id": "qa1"})
        self.assertEqual(201, status)
        status, payload = route_coldchain(self.service, "GET",
                                          "/coldchain/disposition-record?plan_id=plan-api", None)
        self.assertEqual(200, status)
        self.assertEqual("within_budget", payload["latest_outcome"])
        self.assertEqual([], payload["outstanding_obligations"])
        status, payload = route_coldchain(self.service, "GET",
                                          "/coldchain/assessments?plan_id=plan-api", None)
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["items"]))

    def test_unknown_route_and_invalid_body(self):
        status, payload = route_coldchain(self.service, "GET", "/coldchain/missing", None)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])
        status, payload = route_coldchain(self.service, "POST", "/coldchain/plans",
                                          {"request_id": "x1"}, {"X-Actor-Id": "op1"})
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])

    def test_combined_router_falls_back_to_foundation(self):
        status, payload = route_combined(self.foundation, self.service, "GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])
        status, _ = route_combined(self.foundation, self.service, "GET",
                                   "/coldchain/plans?plan_id=none", None)
        self.assertEqual(404, status)


if __name__ == "__main__":
    unittest.main()
