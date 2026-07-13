import unittest

from app.auth import hash_token, verify_bearer_header


class AuthTests(unittest.TestCase):
    def test_bearer_token_verification(self) -> None:
        token = "test-token"
        expected = hash_token(token)

        self.assertTrue(verify_bearer_header(f"Bearer {token}", expected))
        self.assertFalse(verify_bearer_header("Bearer wrong", expected))
        self.assertFalse(verify_bearer_header(None, expected))
