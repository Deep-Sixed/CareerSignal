"""Synthetic tests: draft proposals cannot mutate state or send mail."""

import json
import unittest

from communications.recruiter_proposals import draft_prompt, propose, safety_flags


class Reply:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size):
        return json.dumps({"choices": [{"message": {"content":
            "Thank you for the opportunity. Could you share more details?"}}]}).encode()


class FakeOpener:
    def __init__(self):
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        return Reply()


class ProposalTests(unittest.TestCase):
    def test_prompt_does_not_use_hardcoded_identity(self):
        messages = draft_prompt(recruiter="Recruiter", opportunity="Engineer", company="Company")
        self.assertNotIn("Charles", str(messages))
        self.assertIn("untrusted context", messages[0]["content"])

    def test_input_is_required(self):
        with self.assertRaises(ValueError):
            draft_prompt(recruiter="", opportunity="Engineer", company="")

    def test_proposal_is_local_only_until_explicit_network_opt_in(self):
        op = FakeOpener()
        result = propose(recruiter="Recruiter", opportunity="Engineer",
                         company="Company", endpoint="http://127.0.0.1:4000/v1",
                         api_key="synthetic-test-key", model="local", opener=op)
        self.assertEqual(len(op.calls), 1)
        self.assertIn("/chat/completions", op.calls[0][0].full_url)
        self.assertTrue(result.safe_to_preview)
        self.assertEqual(result.model, "local")
        self.assertEqual(op.calls[0][0].get_method(), "POST")

    def test_no_plain_http_remote_endpoint(self):
        op = FakeOpener()
        with self.assertRaises(ValueError):
            propose(recruiter="Recruiter", opportunity="Engineer", company="Company",
                    endpoint="http://example.org/v1", api_key="test", model="test", opener=op)
        self.assertEqual(op.calls, [])

    def test_flags_sensitive_numbers_and_claims(self):
        flags = safety_flags("Re: opening", "I will send this. My SSN is 123-45-6789.")
        self.assertIn("possible_ssn", flags)
        self.assertIn("contains_send_intent", flags)

    def test_empty_model_reply_is_flagged_not_approved(self):
        self.assertIn("body_too_short", safety_flags("Subject", ""))


if __name__ == "__main__":
    unittest.main()
