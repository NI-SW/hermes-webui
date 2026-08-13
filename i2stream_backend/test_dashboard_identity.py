from __future__ import annotations

import re
import unittest

from fastapi import Response
from pydantic import SecretStr

from dashboard_identity import (
    DASHBOARD_USER_COOKIE_MAX_AGE_SECONDS,
    DASHBOARD_USER_COOKIE_NAME,
    DashboardAnonymousIdentity,
)


class DashboardAnonymousIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.identity = DashboardAnonymousIdentity(
            SecretStr("dashboard-cookie-secret-at-least-32-bytes"),
        )

    def test_missing_cookie_issues_signed_identity_and_persistent_cookie(self) -> None:
        user_id, cookie_value = self.identity.resolve(None)
        response = Response()

        self.identity.set_cookie(response, cookie_value, secure=False)

        self.assertRegex(user_id, re.compile(r"^[0-9a-f]{64}$"))
        self.assertEqual(self.identity.resolve(cookie_value), (user_id, cookie_value))
        set_cookie = response.headers["set-cookie"]
        self.assertIn(f"{DASHBOARD_USER_COOKIE_NAME}={cookie_value}", set_cookie)
        self.assertIn(f"Max-Age={DASHBOARD_USER_COOKIE_MAX_AGE_SECONDS}", set_cookie)
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=lax", set_cookie)
        self.assertNotIn("Secure", set_cookie)

    def test_valid_cookie_keeps_same_user_and_is_renewed_for_https(self) -> None:
        expected_user_id, cookie_value = self.identity.resolve(None)
        response = Response()

        user_id, renewed_cookie = self.identity.resolve(cookie_value)
        self.identity.set_cookie(response, renewed_cookie, secure=True)

        self.assertEqual(user_id, expected_user_id)
        self.assertEqual(renewed_cookie, cookie_value)
        self.assertIn("Secure", response.headers["set-cookie"])

    def test_tampered_cookie_becomes_a_new_anonymous_user(self) -> None:
        old_user_id, cookie_value = self.identity.resolve(None)
        replacement = "0" if cookie_value[0] != "0" else "1"
        tampered = f"{replacement}{cookie_value[1:]}"

        new_user_id, new_cookie_value = self.identity.resolve(tampered)

        self.assertNotEqual(new_user_id, old_user_id)
        self.assertNotEqual(new_cookie_value, tampered)
        self.assertEqual(self.identity.resolve(new_cookie_value), (new_user_id, new_cookie_value))


if __name__ == "__main__":
    unittest.main()
