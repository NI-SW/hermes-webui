from __future__ import annotations

import os
import unittest

from fastapi import HTTPException

os.environ.setdefault("VECTOR_SEARCH_HOST", "http://127.0.0.1:8900")
os.environ.setdefault("SESSION_HMAC_SECRET", "session-secret-32-bytes-for-tests!!")
os.environ.setdefault("GATEWAY_BRIDGE_TOKEN", "gateway-token-32-bytes-for-tests!!!")

from file_store import inbox_upload_from_json


class ReportUploadContractTests(unittest.TestCase):
    def test_node_backend_report_payload_maps_to_media_and_description(self) -> None:
        media, description = inbox_upload_from_json(
            {
                "report": "MEDIA:/app/iadata/report.html",
                "error": 1,
                "warning": 0,
                "summary": "发现一项错误",
            }
        )

        self.assertEqual(media, "MEDIA:/app/iadata/report.html")
        self.assertEqual(description, "发现一项错误")

    def test_node_backend_report_payload_requires_string_report_and_summary(self) -> None:
        with self.assertRaises(HTTPException) as context:
            inbox_upload_from_json(
                {
                    "report": 123,
                    "error": 0,
                    "warning": 0,
                    "summary": "无异常",
                }
            )

        self.assertEqual(context.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
