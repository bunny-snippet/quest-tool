import re
import zipfile
from datetime import datetime, timezone as dt_timezone
from io import BytesIO
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.db import OperationalError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from xml.etree import ElementTree

from accounts.models import AccessFunction, UserFunctionOverride
from surveys.models import Survey, SurveyAttempt, TargetingQuestion
from surveys.providers import ProviderError
from surveys.survey_flow import create_attempt

from .constants import DATABASE_ALIAS
from .models import PrescreenerAnswer, PrescreenerSubmission
from .cache import (
    _namespace_version,
    _options_namespace_version,
    _summary_namespace_version,
    cached_profile,
    invalidate_vault_cache,
    vault_filter_options,
    vault_filtered_summary,
)
from .services import (
    _age_from_value,
    _canonical_attribute,
    capture_prescreener_submission,
    increment_profile_usage,
)
from .services import PrescreenerVaultError


@override_settings(
    PRESCREENER_VAULT_ENABLED=True,
    ALLOW_LEGACY_UNSIGNED_ENTRY_LINKS=True,
)
class PrescreenerVaultFlowTests(TestCase):
    databases = {"default", DATABASE_ALIAS}

    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(
            username="vault-user",
            first_name="Vault",
            last_name="User",
            email="vault@example.test",
        )
        self.survey = Survey.objects.create(
            source_id=801122,
            name="US profile survey",
            status=Survey.Status.LIVE,
            company_name="Example client",
            country="United States",
            country_code="US",
            language="English",
            language_code="EN",
            entry_link="https://provider.example/start?PID=[%%pid%%]",
        )
        self.age = TargetingQuestion.objects.create(
            survey=self.survey,
            question_id=1,
            key="AGE",
            text="What is your age?",
            question_type="Numeric",
            category="Demographic",
        )
        self.gender = TargetingQuestion.objects.create(
            survey=self.survey,
            question_id=2,
            key="GENDER",
            text="What is your gender?",
            question_type="Single Punch",
            category="Demographic",
            options=[
                {"OptionId": 1, "OptionText": "Male"},
                {"OptionId": 2, "OptionText": "Female"},
            ],
        )

    def _attempt(self):
        return create_attempt(self.survey, self.user, "8.8.8.8")

    def _submit(self, attempt, age="24", gender="1"):
        return self.client.post(reverse("survey-start"), {
            "rid": attempt.rid,
            f"question_{self.age.pk}": age,
            f"question_{self.gender.pk}": gender,
        })

    def test_valid_submission_is_saved_only_in_vault_with_profile_snapshots(self):
        attempt = self._attempt()
        response = self._submit(attempt)
        self.assertEqual(response.status_code, 302)

        attempt.refresh_from_db()
        self.assertRegex(attempt.prescreener_uid, r"^[A-Za-z0-9]{4}(?:-[A-Za-z0-9]{4}){3}$")
        self.assertNotEqual(attempt.prescreener_uid.replace("-", "")[:10], attempt.rid)
        self.assertEqual(attempt.answers, {})

        submission = PrescreenerSubmission.objects.using(DATABASE_ALIAS).get(uid=attempt.prescreener_uid)
        self.assertEqual(submission.rid, attempt.rid)
        self.assertEqual(submission.country_code, "US")
        self.assertEqual(submission.language_code, "EN")
        self.assertEqual(submission.respondent_age, 24)
        self.assertEqual(submission.respondent_age_group, "18-24")
        self.assertEqual(submission.respondent_gender, "male")
        self.assertEqual(submission.answer_count, 2)
        self.assertEqual(submission.usage_count, 1)
        self.assertEqual(cached_profile(submission.uid)["usage_count"], 1)
        with self.captureOnCommitCallbacks(using=DATABASE_ALIAS, execute=True):
            self.assertEqual(increment_profile_usage(submission.uid), 2)
        self.assertEqual(cached_profile(submission.uid)["usage_count"], 2)
        submission.refresh_from_db(using=DATABASE_ALIAS)
        self.assertEqual(submission.usage_count, 2)
        gender = PrescreenerAnswer.objects.using(DATABASE_ALIAS).get(
            submission=submission, question_key="GENDER"
        )
        self.assertEqual(gender.question_text, "What is your gender?")
        self.assertEqual(gender.answer_values, ["1"])
        self.assertEqual(gender.answer_labels, ["Male"])
        self.assertEqual(gender.upstream_values, ["1"])

    @patch("surveys.views.resolve_entry_geolocation")
    def test_wrong_entry_country_is_s4_before_prescreener_and_audited(self, geo_lookup):
        geo_lookup.return_value = {
            "ip": "8.8.8.8", "country_code": "IN", "country": "India",
            "postal_code": "110001", "source": "test",
        }
        attempt = self._attempt()

        response = self.client.get(reverse("survey-start"), {"rid": attempt.rid})

        self.assertEqual(response.status_code, 302)
        self.assertIn("status=4", response["Location"])
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, SurveyAttempt.Status.QUALITY_TERMINATED)
        self.assertEqual(attempt.status_source, "local_country_guard")
        self.assertEqual(
            attempt.upstream_transaction_data["local_country_guard"]["reason"],
            "Wrong target country",
        )
        submission = PrescreenerSubmission.objects.using(DATABASE_ALIAS).get(rid=attempt.rid)
        self.assertEqual(submission.respondent_postal_code, "110001")
        self.assertTrue(
            PrescreenerAnswer.objects.using(DATABASE_ALIAS).filter(
                submission=submission,
                question_text="Entry validation result",
                answer_labels=["Wrong target country"],
            ).exists()
        )

    def test_entry_ip_postal_is_added_to_vault_without_a_prescreener_question(self):
        attempt = create_attempt(
            self.survey,
            self.user,
            "8.8.8.8",
            client_data={"geo_country_code": "US", "geo_postal_code": "90210"},
        )

        response = self._submit(attempt)

        self.assertEqual(response.status_code, 302)
        submission = PrescreenerSubmission.objects.using(DATABASE_ALIAS).get(rid=attempt.rid)
        self.assertEqual(submission.respondent_postal_code, "90210")
        self.assertEqual(submission.answer_count, 3)
        self.assertTrue(
            PrescreenerAnswer.objects.using(DATABASE_ALIAS).filter(
                submission=submission,
                question_key="postal_code",
                question_text="Postal code (derived from entry IP)",
            ).exists()
        )

    def test_same_profile_answers_on_new_links_create_distinct_uid_rows(self):
        first = self._attempt()
        second = self._attempt()
        self.assertEqual(self._submit(first).status_code, 302)
        self.assertEqual(self._submit(second).status_code, 302)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertNotEqual(first.rid, second.rid)
        self.assertNotEqual(first.prescreener_uid, second.prescreener_uid)
        self.assertEqual(PrescreenerSubmission.objects.using(DATABASE_ALIAS).count(), 2)

    def test_vault_metadata_and_profile_cache_invalidate_after_write(self):
        self.assertEqual(vault_filter_options()["countries"], [])
        self.assertEqual(vault_filtered_summary({})["total"], 0)

        attempt = self._attempt()
        with self.captureOnCommitCallbacks(using=DATABASE_ALIAS, execute=True):
            self.assertEqual(self._submit(attempt).status_code, 302)
        attempt.refresh_from_db()

        self.assertEqual(vault_filtered_summary({})["total"], 1)
        self.assertEqual(vault_filter_options()["countries"][0]["country_code"], "US")
        profile = cached_profile(attempt.prescreener_uid)
        self.assertEqual(profile["uid"], attempt.prescreener_uid)
        self.assertEqual(profile["respondent_age"], 24)

        invalidate_vault_cache()
        self.assertEqual(cached_profile(attempt.prescreener_uid)["usage_count"], 1)

    def test_repeated_writes_throttle_only_expensive_filter_option_rotation(self):
        profile_before = _namespace_version()
        summary_before = _summary_namespace_version()
        options_before = _options_namespace_version()

        invalidate_vault_cache()
        profile_after_first = _namespace_version()
        summary_after_first = _summary_namespace_version()
        options_after_first = _options_namespace_version()
        invalidate_vault_cache()

        self.assertGreater(profile_after_first, profile_before)
        self.assertGreater(summary_after_first, summary_before)
        self.assertGreater(options_after_first, options_before)
        self.assertGreater(_namespace_version(), profile_after_first)
        self.assertGreater(_summary_namespace_version(), summary_after_first)
        self.assertEqual(_options_namespace_version(), options_after_first)

    def test_single_uid_invalidation_does_not_rotate_unrelated_profile_cache(self):
        with (
            patch("prescreener_vault.cache.safe_cache_delete") as delete_profile,
            patch("prescreener_vault.cache.safe_cache_increment") as increment_generation,
        ):
            invalidate_vault_cache(
                "UID0000000000000001",
                summary=False,
                options=False,
            )

        delete_profile.assert_called_once()
        increment_generation.assert_not_called()

    def test_admin_can_filter_and_expand_prescreened_data_page(self):
        attempt = self._attempt()
        self.assertEqual(self._submit(attempt, age="24", gender="1").status_code, 302)
        admin = get_user_model().objects.create_superuser(
            username="vault-admin", email="vault-admin@example.test", password="test-password"
        )
        self.client.force_login(admin)
        response = self.client.get(reverse("prescreened-data"), {
            "country": "US", "age_group": "18-24", "gender": "male",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Panelist Data")
        self.assertContains(response, "Country / Language")
        self.assertContains(response, "Profile Specs")
        self.assertContains(response, "Registered at")
        self.assertContains(response, "Visits")
        self.assertContains(response, "Profile Information")
        self.assertContains(response, "Profile details")
        self.assertNotContains(response, attempt.rid)
        self.assertContains(response, "What is your age?")
        self.assertContains(response, "Male")
        self.assertContains(response, "All countries")
        self.assertContains(response, "vault-answer-drawer")
        self.assertNotContains(response, "<details")

        rid_search = self.client.get(reverse("prescreened-data"), {
            "search": attempt.rid,
        })
        self.assertContains(rid_search, "No profiles available")
        uid_search = self.client.get(reverse("prescreened-data"), {
            "search": PrescreenerSubmission.objects.using(DATABASE_ALIAS).get(
                rid=attempt.rid
            ).uid,
        })
        self.assertContains(uid_search, "Profile details")

        exported = self.client.get(reverse("prescreened-data-export"), {
            "country": "US", "age_group": "18-24", "gender": "male",
        })
        self.assertEqual(exported.status_code, 200)
        self.assertIn(".xlsx", exported["Content-Disposition"])
        content = b"".join(exported.streaming_content)
        with zipfile.ZipFile(BytesIO(content)) as workbook:
            self.assertIn("xl/worksheets/sheet2.xml", workbook.namelist())
            submissions = ElementTree.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
            answers = ElementTree.fromstring(workbook.read("xl/worksheets/sheet2.xml"))
        submission_text = " ".join(submissions.itertext())
        answer_text = " ".join(answers.itertext())
        self.assertNotIn(attempt.rid, submission_text)
        self.assertNotIn(attempt.rid, answer_text)
        self.assertNotIn("RID", submission_text)
        self.assertNotIn("RID", answer_text)
        self.assertNotIn("Answer count", submission_text)
        self.assertIn("Visits", submission_text)
        self.assertIn("What is your age?", answer_text)
        self.assertIn("Male", answer_text)

    def test_panelist_mobile_markup_and_export_follow_column_permissions(self):
        attempt = self._attempt()
        self.assertEqual(self._submit(attempt, age="24", gender="1").status_code, 302)
        viewer = get_user_model().objects.create_user(username="vault-scoped")
        for code in (
            "prescreener_data.view",
            "prescreener_data.export",
            "prescreener_data.column.usage_count",
        ):
            UserFunctionOverride.objects.create(
                user=viewer,
                function=AccessFunction.objects.get(code=code),
                effect=UserFunctionOverride.Effect.ALLOW,
            )
        self.client.force_login(viewer)

        page = self.client.get(reverse("prescreened-data"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Visits")
        self.assertNotContains(page, attempt.prescreener_uid)
        self.assertNotContains(page, "Profile details")
        self.assertNotContains(page, "What is your age?")

        exported = self.client.get(reverse("prescreened-data-export"))
        content = b"".join(exported.streaming_content)
        with zipfile.ZipFile(BytesIO(content)) as workbook:
            self.assertNotIn("xl/worksheets/sheet2.xml", workbook.namelist())
            submissions = ElementTree.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
        submission_text = " ".join(submissions.itertext())
        self.assertIn("Visits", submission_text)
        self.assertNotIn("UID", submission_text)
        self.assertNotIn(attempt.prescreener_uid, submission_text)

        UserFunctionOverride.objects.create(
            user=viewer,
            function=AccessFunction.objects.get(code="prescreener_data.column.answers"),
            effect=UserFunctionOverride.Effect.ALLOW,
        )
        answer_page = self.client.get(reverse("prescreened-data"))
        self.assertContains(answer_page, "What is your age?")
        self.assertNotContains(answer_page, attempt.prescreener_uid)

    def test_rfg_birthday_alias_and_display_date_are_normalized_to_age(self):
        submitted_at = datetime(2026, 8, 13, 12, tzinfo=dt_timezone.utc)
        self.assertEqual(
            _canonical_attribute("RFG_BIRTHDAY", "What is your date of birth?"),
            "date_of_birth",
        )
        self.assertEqual(_age_from_value("13-08-2001", submitted_at), 25)
        self.assertEqual(_age_from_value("2001-08-13", submitted_at), 25)

    def test_repair_command_permanently_rebuilds_old_rfg_profile_specs(self):
        submission = PrescreenerSubmission.objects.using(DATABASE_ALIAS).create(
            uid="OldR-FG00-Prof-0001",
            rid="OldRfg1234",
            country="United States",
            country_code="US",
            language="English",
            language_code="EN",
            submitted_at=timezone.now(),
        )
        answer = PrescreenerAnswer.objects.using(DATABASE_ALIAS).create(
            submission=submission,
            position=1,
            question_key="RFG_BIRTHDAY",
            question_text="What is your date of birth?",
            answer_values=["13-08-2001"],
        )

        call_command("repair_panelist_profiles", stdout=StringIO())

        submission.refresh_from_db(using=DATABASE_ALIAS)
        answer.refresh_from_db(using=DATABASE_ALIAS)
        self.assertEqual(answer.canonical_attribute, "date_of_birth")
        self.assertIsNotNone(submission.respondent_age)
        self.assertTrue(submission.respondent_age_group)

    def test_vault_failure_does_not_redirect_or_lose_the_retry(self):
        attempt = self._attempt()
        with patch(
            "surveys.views.capture_prescreener_submission",
            side_effect=PrescreenerVaultError("database unavailable"),
        ):
            response = self._submit(attempt)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Secure prescreener storage is temporarily unavailable")
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, SurveyAttempt.Status.INITIATED)
        self.assertEqual(attempt.answers, {})

    def test_provider_failure_allows_corrected_answers_on_same_rid_retry(self):
        attempt = self._attempt()
        with patch(
            "surveys.views.build_outbound_url",
            side_effect=ProviderError("provider temporarily unavailable"),
        ):
            first = self._submit(attempt, age="24", gender="1")
        self.assertEqual(first.status_code, 200)
        self.assertContains(first, "provider temporarily unavailable")
        original = PrescreenerSubmission.objects.using(DATABASE_ALIAS).get(rid=attempt.rid)
        self.assertEqual(original.respondent_age, 24)

        second = self._submit(attempt, age="25", gender="1")

        self.assertEqual(second.status_code, 302)
        corrected = PrescreenerSubmission.objects.using(DATABASE_ALIAS).get(rid=attempt.rid)
        self.assertEqual(corrected.uid, original.uid)
        self.assertEqual(corrected.respondent_age, 25)
        self.assertEqual(corrected.usage_count, 1)

    @patch("prescreener_vault.services.time.sleep")
    def test_transient_mysql_lock_timeout_is_retried(self, sleep_mock):
        attempt = self._attempt()
        answers = {
            str(self.age.pk): {
                "question_id": self.age.question_id,
                "question_key": self.age.key,
                "question_text": self.age.text,
                "values": ["24"],
                "upstream_values": ["24"],
            }
        }
        real_atomic = transaction.atomic
        call_count = 0

        def flaky_atomic(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OperationalError(1205, "Lock wait timeout exceeded")
            return real_atomic(*args, **kwargs)

        with patch("prescreener_vault.services.transaction.atomic", side_effect=flaky_atomic):
            submission, created = capture_prescreener_submission(attempt, answers)

        self.assertTrue(created)
        self.assertEqual(submission.rid, attempt.rid)
        # The second top-level attempt succeeds. ``bulk_create`` may open
        # additional internal atomic blocks depending on the DB backend.
        self.assertGreaterEqual(call_count, 2)
        sleep_mock.assert_called_once()
        self.assertGreaterEqual(sleep_mock.call_args.args[0], 0.0375)
        self.assertLessEqual(sleep_mock.call_args.args[0], 0.0625)

    def test_backfill_can_verify_then_clear_existing_operational_answers(self):
        attempt = SurveyAttempt.objects.create(
            rid="Abc123Xyz9",
            survey=self.survey,
            platform_user=self.user,
            user_id=str(self.user.pk),
            answers={
                str(self.gender.pk): {
                    "question_id": self.gender.question_id,
                    "question_key": self.gender.key,
                    "question_text": self.gender.text,
                    "values": ["2"],
                    "upstream_values": ["2"],
                }
            },
        )
        output = StringIO()
        call_command("backfill_prescreener_vault", "--clear-source", stdout=output)
        attempt.refresh_from_db()
        self.assertTrue(re.fullmatch(r"[A-Za-z0-9]{4}(?:-[A-Za-z0-9]{4}){3}", attempt.prescreener_uid))
        self.assertEqual(attempt.answers, {})
        self.assertTrue(
            PrescreenerSubmission.objects.using(DATABASE_ALIAS).filter(
                uid=attempt.prescreener_uid, rid=attempt.rid
            ).exists()
        )
        self.assertIn("failed=0", output.getvalue())

    def test_reconcile_command_repairs_answers_and_classifies_the_remaining_gap(self):
        synced = self._attempt()
        self.assertEqual(self._submit(synced).status_code, 302)
        pending = self._attempt()
        recoverable = SurveyAttempt.objects.create(
            rid="Rec123AbC9",
            survey=self.survey,
            platform_user=self.user,
            user_id=str(self.user.pk),
            answers={
                str(self.gender.pk): {
                    "question_id": self.gender.question_id,
                    "question_key": self.gender.key,
                    "question_text": self.gender.text,
                    "values": ["2"],
                    "upstream_values": ["2"],
                }
            },
        )
        lost = SurveyAttempt.objects.create(
            rid="Lost12AbC9",
            survey=self.survey,
            platform_user=self.user,
            user_id=str(self.user.pk),
            status=SurveyAttempt.Status.REDIRECTED,
            submitted_at=timezone.now(),
            redirected_at=timezone.now(),
            answers={},
        )

        output = StringIO()
        call_command(
            "reconcile_prescreener_vault",
            "--repair",
            "--show-missing=10",
            stdout=output,
        )

        recoverable.refresh_from_db()
        self.assertTrue(
            PrescreenerSubmission.objects.using(DATABASE_ALIAS).filter(
                rid=recoverable.rid,
                uid=recoverable.prescreener_uid,
            ).exists()
        )
        self.assertFalse(
            PrescreenerSubmission.objects.using(DATABASE_ALIAS).filter(rid=pending.rid).exists()
        )
        self.assertFalse(
            PrescreenerSubmission.objects.using(DATABASE_ALIAS).filter(rid=lost.rid).exists()
        )
        report = output.getvalue()
        self.assertIn('"linked": 1', report)
        self.assertIn('"repairable": 1', report)
        self.assertIn('"repaired": 1', report)
        self.assertIn('"not_submitted": 1', report)
        self.assertIn('"submitted_payload_missing": 1', report)
