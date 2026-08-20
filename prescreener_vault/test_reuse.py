from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from surveys.models import (
    ProfileReuseEvent,
    ProfileReuseState,
    Survey,
    SurveyAttempt,
    TargetingQuestion,
)
from surveys.serializers import SurveyAttemptSerializer
from surveys.survey_flow import create_attempt
from vendors.models import Client, ClientIntegration

from .constants import DATABASE_ALIAS
from .models import PrescreenerSubmission
from .reuse import maybe_assign_reusable_profile, profile_reuse_month_status


@override_settings(PRESCREENER_VAULT_ENABLED=True)
class ReusableProfileQueueTests(TestCase):
    databases = {"default", DATABASE_ALIAS}

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="reuse-user")
        client = Client.objects.create(code="reuse-client", name="Reuse client")
        self.integration = ClientIntegration.objects.create(
            client=client,
            name="Reuse production",
            provider_code="rfg",
            base_url="https://provider.example/api",
            profile_reuse_enabled=True,
            profile_reuse_eligible_after_days=30,
            profile_reuse_monthly_percentage=100,
            profile_reuse_country_codes=["US"],
            profile_reuse_age_groups=["18-24"],
            profile_reuse_genders=["male"],
            profile_rereuse_enabled=True,
            profile_rereuse_percentage=50,
            profile_rereuse_cooldown_days=30,
        )
        self.survey = Survey.objects.create(
            integration=self.integration,
            source_id=99101,
            name="Reuse survey",
            status=Survey.Status.LIVE,
            company_name="Reuse client",
            country="United States",
            country_code="US",
            language="English",
            language_code="EN",
            entry_link="https://provider.example/start",
        )
        self.age = TargetingQuestion.objects.create(
            survey=self.survey, question_id=1, key="AGE", text="What is your age?",
            question_type="Numeric", category="Demographic",
        )
        self.gender = TargetingQuestion.objects.create(
            survey=self.survey, question_id=2, key="GENDER", text="What is your gender?",
            question_type="Single Punch", category="Demographic",
            options=[{"OptionId": 1, "OptionText": "Male"}, {"OptionId": 2, "OptionText": "Female"}],
        )
        current_month = timezone.localtime().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        previous_month = current_month - timedelta(days=1)
        for _ in range(10):
            baseline = create_attempt(self.survey, self.user, "8.8.8.8")
            SurveyAttempt.objects.filter(pk=baseline.pk).update(initiated_at=previous_month)

    def answers(self, age="23", gender="1"):
        return {
            str(self.age.pk): {"values": [age], "upstream_values": [age]},
            str(self.gender.pk): {"values": [gender], "upstream_values": [gender]},
        }

    def candidate(
        self, uid, rid, *, submitted_days=90, usage_count=1,
        source_client_code=None, last_reused_days=None, profile_dimensions=None,
        postal_code="",
    ):
        return PrescreenerSubmission.objects.using(DATABASE_ALIAS).create(
            uid=uid,
            rid=rid,
            source_client_code=source_client_code or self.integration.client.code,
            country="United States",
            country_code="US",
            language="English",
            language_code="EN",
            respondent_age=23,
            respondent_age_group="18-24",
            respondent_gender="male",
            respondent_postal_code=postal_code,
            profile_dimensions=profile_dimensions or {},
            usage_count=usage_count,
            last_reused_at=(
                timezone.now() - timedelta(days=last_reused_days)
                if last_reused_days is not None else None
            ),
            submitted_at=timezone.now() - timedelta(days=submitted_days),
        )

    def another_survey(self, source_id):
        survey = Survey.objects.create(
            integration=self.integration,
            source_id=source_id,
            name=f"Reuse survey {source_id}",
            status=Survey.Status.LIVE,
            company_name="Reuse client",
            country="United States",
            country_code="US",
            language="English",
            language_code="EN",
            entry_link="https://provider.example/start",
        )
        age = TargetingQuestion.objects.create(
            survey=survey, question_id=1, key="AGE", text="What is your age?",
            question_type="Numeric", category="Demographic",
        )
        gender = TargetingQuestion.objects.create(
            survey=survey, question_id=2, key="GENDER", text="What is your gender?",
            question_type="Single Punch", category="Demographic",
            options=[{"OptionId": 1, "OptionText": "Male"}],
        )
        answers = {
            str(age.pk): {"values": ["23"], "upstream_values": ["23"]},
            str(gender.pk): {"values": ["1"], "upstream_values": ["1"]},
        }
        return survey, answers

    def expire_minimum_gap(self, candidate):
        old = timezone.now() - timedelta(minutes=2)
        PrescreenerSubmission.objects.using(DATABASE_ALIAS).filter(pk=candidate.pk).update(
            last_reused_at=old
        )
        ProfileReuseState.objects.filter(
            integration=self.integration, reused_uid=candidate.uid
        ).update(last_reused_at=old)

    def test_monthly_target_is_split_between_first_and_returning_pools(self):
        fresh = self.candidate("Aa11-Bb22-Cc33-Dd44", "OldRid0001", submitted_days=100)
        returning = self.candidate(
            "Ee55-Ff66-Gg77-Hh88", "OldRid0002", submitted_days=120,
            usage_count=2, last_reused_days=60,
        )

        first_attempt = create_attempt(self.survey, self.user, "8.8.8.8")
        first_event = maybe_assign_reusable_profile(first_attempt, self.answers())
        second_attempt = create_attempt(self.survey, self.user, "8.8.8.8")
        second_event = maybe_assign_reusable_profile(second_attempt, self.answers())

        self.assertEqual((first_event.reuse_pool, first_event.reused_uid), ("first", fresh.uid))
        self.assertEqual(
            (second_event.reuse_pool, second_event.reused_uid),
            ("returning", returning.uid),
        )
        status = profile_reuse_month_status(self.integration)
        self.assertEqual(status["first_reuse_target"], 5)
        self.assertEqual(status["repeat_reuse_target"], 5)
        self.assertEqual(status["first_reuse_used"], 1)
        self.assertEqual(status["repeat_reuse_used"], 1)

    def test_returning_pool_uses_lowest_visit_round_first(self):
        lower = self.candidate(
            "Ii11-Jj22-Kk33-Ll44", "OldRid0005", usage_count=2,
            submitted_days=120, last_reused_days=60,
        )
        self.candidate(
            "Mm55-Nn66-Oo77-Pp88", "OldRid0006", usage_count=3,
            submitted_days=130, last_reused_days=70,
        )
        # Consume the first-pool slot selector attempt so the next proportional
        # slot belongs to the returning pool.
        self.candidate("Qq11-Rr22-Ss33-Tt44", "OldRid0007", submitted_days=100)
        first_attempt = create_attempt(self.survey, self.user, "8.8.8.8")
        self.assertEqual(
            maybe_assign_reusable_profile(first_attempt, self.answers()).reuse_pool,
            "first",
        )
        returning_attempt = create_attempt(self.survey, self.user, "8.8.8.8")
        event = maybe_assign_reusable_profile(returning_attempt, self.answers())
        self.assertEqual((event.reuse_pool, event.reused_uid), ("returning", lower.uid))

    def test_profile_never_crosses_client_boundary(self):
        other_client = Client.objects.create(code="other-client", name="Other client")
        self.candidate(
            "Uv11-Wx22-Yz33-Ab44", "OldRid0010", submitted_days=100,
            source_client_code=other_client.code,
        )
        attempt = create_attempt(self.survey, self.user, "8.8.8.8")
        self.assertIsNone(maybe_assign_reusable_profile(attempt, self.answers()))
        self.assertEqual(ProfileReuseEvent.objects.count(), 0)

    def test_reuse_keeps_original_vault_pair_and_only_increments_visits(self):
        candidate = self.candidate(
            "Qq11-Rr22-Ss33-Tt44", "OldRid0099", submitted_days=90
        )
        attempt = create_attempt(self.survey, self.user, "8.8.8.8")
        original_count = PrescreenerSubmission.objects.using(DATABASE_ALIAS).count()

        event = maybe_assign_reusable_profile(attempt, self.answers())
        self.assertIsNotNone(event)
        # The prescreener submit path skips capture_prescreener_submission when
        # an event exists, so there is no second panelist profile row.
        self.assertEqual(
            PrescreenerSubmission.objects.using(DATABASE_ALIAS).count(), original_count
        )
        candidate.refresh_from_db(using=DATABASE_ALIAS)
        self.assertEqual((event.reused_rid, event.reused_uid), (candidate.rid, candidate.uid))
        self.assertEqual(candidate.usage_count, 2)

    def test_reused_profile_has_a_new_traffic_journey_row(self):
        candidate = self.candidate(
            "Tr11-Af22-Fi33-Cu44", "OldRid0042", submitted_days=90
        )
        attempt = create_attempt(self.survey, self.user, "8.8.8.8")
        registered_uid = attempt.prescreener_uid

        event = maybe_assign_reusable_profile(attempt, self.answers())
        attempt.refresh_from_db()
        traffic_row = SurveyAttemptSerializer(attempt).data

        self.assertNotEqual(attempt.rid, candidate.rid)
        self.assertEqual(event.reused_rid, candidate.rid)
        self.assertEqual(traffic_row["rid"], attempt.rid)
        self.assertEqual(traffic_row["prescreener_uid"], candidate.uid)
        self.assertEqual(traffic_row["registered_profile_uid"], registered_uid)
        self.assertTrue(traffic_row["profile_was_reused"])

        retry_event = maybe_assign_reusable_profile(attempt, self.answers())
        candidate.refresh_from_db(using=DATABASE_ALIAS)
        self.assertEqual(retry_event.pk, event.pk)
        self.assertEqual(candidate.usage_count, 2)

    def test_days_demographics_and_monthly_budget_are_enforced(self):
        self.candidate("Ii99-Jj00-Kk11-Ll22", "OldRid0003", submitted_days=5)
        attempt = create_attempt(self.survey, self.user, "8.8.8.8")
        self.assertIsNone(maybe_assign_reusable_profile(attempt, self.answers()))
        self.assertEqual(profile_reuse_month_status(self.integration)["used_reuses"], 0)

        self.candidate("Mm33-Nn44-Oo55-Pp66", "OldRid0004", submitted_days=90)
        self.integration.profile_reuse_monthly_percentage = 10
        self.integration.profile_rereuse_enabled = False
        self.integration.save(update_fields=[
            "profile_reuse_monthly_percentage", "profile_rereuse_enabled",
        ])
        allowed = create_attempt(self.survey, self.user, "8.8.8.8")
        self.assertIsNotNone(maybe_assign_reusable_profile(allowed, self.answers()))
        exhausted = create_attempt(self.survey, self.user, "8.8.8.8")
        self.assertIsNone(maybe_assign_reusable_profile(exhausted, self.answers()))
        self.assertEqual(profile_reuse_month_status(self.integration)["target_reuses"], 1)

    def test_first_month_uses_current_attempts_as_rolling_bootstrap(self):
        client = Client.objects.create(code="new-client", name="New client")
        integration = ClientIntegration.objects.create(
            client=client,
            name="New production",
            provider_code="cint",
            base_url="https://new-provider.example/api",
            profile_reuse_enabled=True,
            profile_reuse_eligible_after_days=1,
            profile_reuse_monthly_percentage=100,
            profile_reuse_country_codes=["US"],
            profile_reuse_age_groups=["18-24"],
            profile_reuse_genders=["male"],
        )
        survey = Survey.objects.create(
            integration=integration,
            source_id=99102,
            name="First month survey",
            status=Survey.Status.LIVE,
            company_name="New client",
            country="United States",
            country_code="US",
            language="English",
            language_code="EN",
            entry_link="https://new-provider.example/start",
        )
        age = TargetingQuestion.objects.create(
            survey=survey, question_id=1, key="AGE", text="What is your age?",
            question_type="Numeric", category="Demographic",
        )
        gender = TargetingQuestion.objects.create(
            survey=survey, question_id=2, key="GENDER", text="What is your gender?",
            question_type="Single Punch", category="Demographic",
            options=[{"OptionId": 1, "OptionText": "Male"}],
        )
        self.candidate(
            "Zz11-Yy22-Xx33-Ww44", "OldRid0098", submitted_days=90,
            source_client_code=client.code,
        )
        attempt = create_attempt(survey, self.user, "8.8.8.8")
        answers = {
            str(age.pk): {"values": ["23"], "upstream_values": ["23"]},
            str(gender.pk): {"values": ["1"], "upstream_values": ["1"]},
        }

        event = maybe_assign_reusable_profile(attempt, answers)

        self.assertIsNotNone(event)
        status = profile_reuse_month_status(integration)
        self.assertEqual(status["previous_month_attempts"], 0)
        self.assertEqual(status["baseline_source"], "current_month_bootstrap")
        self.assertEqual(status["baseline_attempts"], 1)
        self.assertEqual(status["target_reuses"], 1)
        self.assertEqual(status["used_reuses"], 1)

    def test_uid_is_permanently_blocked_from_the_same_project(self):
        self.integration.profile_reuse_first_delay_minutes = 1
        self.integration.profile_reuse_min_interval_minutes = 1
        self.integration.profile_reuse_max_uses_per_window = 10
        self.integration.save(update_fields=[
            "profile_reuse_first_delay_minutes", "profile_reuse_min_interval_minutes",
            "profile_reuse_max_uses_per_window",
        ])
        candidate = self.candidate("Sa11-Me22-Pr33-Oj44", "OldRid1001")
        first = create_attempt(self.survey, self.user, "8.8.8.8")
        self.assertIsNotNone(maybe_assign_reusable_profile(first, self.answers()))
        self.expire_minimum_gap(candidate)

        duplicate_project = create_attempt(self.survey, self.user, "8.8.8.8")
        self.assertIsNone(maybe_assign_reusable_profile(duplicate_project, self.answers()))
        candidate.refresh_from_db(using=DATABASE_ALIAS)
        self.assertEqual(candidate.usage_count, 2)

    def test_original_registration_project_is_also_permanently_blocked(self):
        self.integration.profile_reuse_first_delay_minutes = 1
        self.integration.profile_reuse_min_interval_minutes = 1
        self.integration.save(update_fields=[
            "profile_reuse_first_delay_minutes", "profile_reuse_min_interval_minutes",
        ])
        candidate = self.candidate("Or11-Ig22-In33-Al44", "OldRid1005")
        original = create_attempt(self.survey, self.user, "8.8.8.8")
        SurveyAttempt.objects.filter(pk=original.pk).update(
            prescreener_uid=candidate.uid,
            provider_profile_uid=candidate.uid,
        )

        same_project = create_attempt(self.survey, self.user, "8.8.8.8")
        self.assertIsNone(maybe_assign_reusable_profile(same_project, self.answers()))
        candidate.refresh_from_db(using=DATABASE_ALIAS)
        self.assertEqual(candidate.usage_count, 1)

    def test_uid_can_reuse_on_different_projects_after_configured_gap(self):
        self.integration.profile_reuse_first_delay_minutes = 1
        self.integration.profile_reuse_min_interval_minutes = 1
        self.integration.profile_reuse_max_uses_per_window = 5
        self.integration.save(update_fields=[
            "profile_reuse_first_delay_minutes", "profile_reuse_min_interval_minutes",
            "profile_reuse_max_uses_per_window",
        ])
        candidate = self.candidate("Di11-Ff22-Pr33-Oj44", "OldRid1002")
        first = create_attempt(self.survey, self.user, "8.8.8.8")
        self.assertIsNotNone(maybe_assign_reusable_profile(first, self.answers()))
        self.expire_minimum_gap(candidate)
        second_survey, second_answers = self.another_survey(99103)

        second = create_attempt(second_survey, self.user, "8.8.8.8")
        event = maybe_assign_reusable_profile(second, second_answers)
        self.assertEqual(event.reused_uid, candidate.uid)
        self.assertNotEqual(first.rid, second.rid)

    def test_rolling_window_limit_is_enforced_across_different_projects(self):
        self.integration.profile_reuse_first_delay_minutes = 1
        self.integration.profile_reuse_min_interval_minutes = 1
        self.integration.profile_reuse_max_uses_per_window = 2
        self.integration.profile_reuse_window_minutes = 1440
        self.integration.save(update_fields=[
            "profile_reuse_first_delay_minutes", "profile_reuse_min_interval_minutes",
            "profile_reuse_max_uses_per_window", "profile_reuse_window_minutes",
        ])
        candidate = self.candidate("Wi11-Nd22-Ow33-Li44", "OldRid1003")
        first = create_attempt(self.survey, self.user, "8.8.8.8")
        self.assertIsNotNone(maybe_assign_reusable_profile(first, self.answers()))
        self.expire_minimum_gap(candidate)
        second_survey, second_answers = self.another_survey(99104)
        second = create_attempt(second_survey, self.user, "8.8.8.8")
        self.assertIsNotNone(maybe_assign_reusable_profile(second, second_answers))
        self.expire_minimum_gap(candidate)
        third_survey, third_answers = self.another_survey(99105)
        third = create_attempt(third_survey, self.user, "8.8.8.8")

        self.assertIsNone(maybe_assign_reusable_profile(third, third_answers))
        self.assertEqual(ProfileReuseEvent.objects.filter(reused_uid=candidate.uid).count(), 2)

    def test_every_mapped_requirement_must_match_or_fresh_uid_is_kept(self):
        self.integration.profile_reuse_first_delay_minutes = 1
        self.integration.save(update_fields=["profile_reuse_first_delay_minutes"])
        postal = TargetingQuestion.objects.create(
            survey=self.survey, question_id=3, key="ZIP", text="What is your postal code?",
            question_type="Text", category="Demographic",
            raw_data={"canonical_key": "postal-code"},
        )
        candidate = self.candidate(
            "Ma11-Tc22-Ha33-Ll44", "OldRid1004", postal_code="3000"
        )
        answers = self.answers()
        answers[str(postal.pk)] = {"values": ["9000"], "upstream_values": ["9000"]}
        attempt = create_attempt(self.survey, self.user, "8.8.8.8")

        self.assertIsNone(maybe_assign_reusable_profile(attempt, answers))
        attempt.refresh_from_db()
        self.assertEqual(attempt.provider_profile_uid, "")
        PrescreenerSubmission.objects.using(DATABASE_ALIAS).filter(pk=candidate.pk).update(
            respondent_postal_code="9000"
        )
        event = maybe_assign_reusable_profile(attempt, answers)
        self.assertEqual(event.reused_uid, candidate.uid)
