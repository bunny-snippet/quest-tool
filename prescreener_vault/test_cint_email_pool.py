import hashlib
from io import StringIO

from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from .cint_email_pool import (
    CintEmailPoolExhausted,
    add_real_email,
    assigned_email_hash,
    email_pool_status,
    reveal_email,
)
from .constants import DATABASE_ALIAS
from .models import CintRespondentEmail, CintRespondentEmailUse


@override_settings(
    RESPONDENT_EMAIL_ENCRYPTION_KEY="test-only-stable-respondent-key",
    CINT_EMAIL_IDENTITY_CACHE_TTL_SECONDS=3600,
)
class CintEmailPoolTests(TestCase):
    databases = {"default", DATABASE_ALIAS}

    def setUp(self):
        cache.clear()

    def test_real_email_is_encrypted_and_normalized_duplicates_are_idempotent(self):
        identity, created = add_real_email("Example.User+survey@gmail.com")
        duplicate, duplicate_created = add_real_email("exampleuser@googlemail.com")

        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(identity.pk, duplicate.pk)
        self.assertNotIn("exampleuser", identity.encrypted_email)
        self.assertEqual(reveal_email(identity), "exampleuser@gmail.com")
        self.assertEqual(
            identity.email_hash,
            hashlib.sha256(b"exampleuser@gmail.com").hexdigest(),
        )

    def test_uid_assignment_is_stable_exclusive_and_usage_is_per_rid(self):
        first, _ = add_real_email("first.real@example.com")
        second, _ = add_real_email("second.real@example.com")
        uid_one = "Ab12-Cd34-Ef56-Gh78"
        uid_two = "Zy98-Xw76-Vu54-Ts32"

        first_hash = assigned_email_hash(uid_one, "Ab3dE5fG7h")
        cache.clear()
        retry_hash = assigned_email_hash(uid_one, "Ab3dE5fG7h")
        new_session_hash = assigned_email_hash(uid_one, "Zx9Yw7Vu5T")
        second_hash = assigned_email_hash(uid_two, "Aa1Bb2Cc3D")

        self.assertEqual(first_hash, retry_hash)
        self.assertEqual(first_hash, new_session_hash)
        self.assertNotEqual(first_hash, second_hash)
        first.refresh_from_db(using=DATABASE_ALIAS)
        second.refresh_from_db(using=DATABASE_ALIAS)
        self.assertEqual(first.assigned_uid, uid_one)
        self.assertEqual(second.assigned_uid, uid_two)
        self.assertEqual(first.use_count, 2)
        self.assertEqual(second.use_count, 1)
        self.assertEqual(CintRespondentEmailUse.objects.using(DATABASE_ALIAS).count(), 3)

        with self.assertRaises(CintEmailPoolExhausted):
            assigned_email_hash("Lm12-No34-Pq56-Rs78", "Qw1Er2Ty3U")

    def test_management_command_imports_without_echoing_addresses(self):
        output = StringIO()
        call_command(
            "cint_email_pool",
            "--add",
            "real.one@example.com",
            "--add",
            "real.two@example.com",
            "--status",
            stdout=output,
        )
        rendered = output.getvalue()
        self.assertIn("processed=2 added=2", rendered)
        self.assertIn("total=2 available=2", rendered)
        self.assertNotIn("real.one", rendered)
        self.assertEqual(email_pool_status()["total"], 2)

    def test_management_command_rejects_invalid_input_without_revealing_it(self):
        output = StringIO()
        errors = StringIO()
        with self.assertRaises(CommandError):
            call_command(
                "cint_email_pool",
                "--add",
                "not-an-email",
                stdout=output,
                stderr=errors,
            )
        self.assertNotIn("not-an-email", output.getvalue() + errors.getvalue())
