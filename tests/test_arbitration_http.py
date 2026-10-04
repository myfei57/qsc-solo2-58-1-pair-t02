"""HTTP-level integration tests for the arbitration endpoints."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from urllib.parse import quote
from urllib.request import Request as UrlRequest, urlopen
from urllib.error import HTTPError

from portpower.bootstrap import create_server
from portpower.config import Settings


def _post(url: str, payload: dict | None = None, method: str = "POST") -> tuple[int, dict]:
    data = json.dumps(payload or {}).encode("utf-8")
    request = UrlRequest(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    try:
        with urlopen(request) as response:
            return response.status, json.loads(response.read())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _get(url: str) -> tuple[int, dict]:
    with urlopen(url) as response:
        return response.status, json.loads(response.read())


class ArbitrationHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp()
        settings = Settings(data_dir=self.dir, host="127.0.0.1", port=0)
        self.server = create_server(settings)
        # port=0 -> bind an ephemeral port, then read the chosen one
        self.server.start()
        host, port = self.server.address
        self.base = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.stop()

    def test_full_conflict_bump_timeout_rebook_traceability_flow(self):
        base = self.base

        # 1. A future reservation for A1
        status, body = _post(f"{base}/berth/booking", {
            "ship": "预约船", "berth": "A1",
            "start": "2030-01-01T09:00:00+00:00",
            "end": "2030-01-01T11:00:00+00:00",
        })
        self.assertEqual(status, 200, body)
        self.assertEqual(body["decision"]["rule"], "R1")
        booking_id = body["booking"]["booking_id"]

        # 2. occupy A2..C1 (catalog has 5 berths) so the walk-in hits A1
        for berth in ("A2", "B1", "B2", "C1"):
            status, body = _post(f"{base}/berth/booking", {
                "ship": f"占位-{berth}", "berth": berth,
                "start": "2030-01-01T08:00:00+00:00",
                "end": "2030-01-02T08:00:00+00:00",
            })
            self.assertEqual(status, 200, body)

        # 3. walk-in pre-empts the future booking (R22)
        status, body = _post(f"{base}/berth/walkin", {"ship": "临时船", "queue": False})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["decision"]["outcome"], "preempted")
        self.assertEqual(body["decision"]["rule"], "R22")
        self.assertEqual(body["decision"]["berth"], "A1")
        self.assertIn("rule_text", body["decision"])

        # 4. displaced reservation is visible as bumped + notice delivered
        status, body = _get(f"{base}/berth/notices?ship={quote('预约船')}")
        self.assertEqual(body["notices"][0]["kind"], "booking_bumped")
        notice_id = body["notices"][0]["notice_id"]
        status, body = _post(f"{base}/berth/notices/delivered", {"notice": notice_id})
        self.assertTrue(body["delivered"])

        # 5. rearrange the displaced reservation onto B1 later window:
        #    cancel B1's placeholder booking first, then rebook
        status, body = _get(f"{base}/berth/bookings?berth=B1&state=held")
        b1_booking = body["bookings"][0]["booking_id"]
        status, body = _post(f"{base}/berth/booking/cancel", {"booking": b1_booking})
        self.assertEqual(status, 200, body)
        status, body = _post(f"{base}/berth/booking/rebook", {
            "booking": booking_id,
            "berth": "B1",
            "start": "2030-01-01T12:00:00+00:00",
            "end": "2030-01-01T14:00:00+00:00",
        })
        self.assertEqual(status, 200, body)
        self.assertEqual(body["decision"]["rule"], "R4")
        self.assertEqual(body["booking"]["state"], "held")
        self.assertEqual(body["booking"]["berth"], "B1")

        # 6. audit trail explains every seizure with rule + instant
        status, body = _get(f"{base}/audit?event=berth.arbitration")
        rules = {entry["detail"].split()[0] for entry in body["entries"]}
        self.assertIn("rule=R1", rules)
        self.assertIn("rule=R22", rules)
        self.assertIn("rule=R4", rules)
        self.assertIn("rule=R3", rules)

    def test_legacy_reservation_api_migrates_and_shares_arbitration(self):
        base = self.base
        status, body = _post(f"{base}/reservation", {
            "ship": "老预约", "berth": "A1",
            "start": "2031-03-03T09:00:00+00:00",
            "end": "2031-03-03T10:00:00+00:00",
        })
        self.assertEqual(status, 200, body)
        status, body = _get(f"{base}/berth/bookings?state=held")
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["bookings"][0]["ship"], "老预约")


if __name__ == "__main__":
    unittest.main()
