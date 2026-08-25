from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from vendors.models import Client, ClientIntegration

from .models import Survey
from .providers.rfg import ResearchForGoodProvider
from .serializers import TargetingQuestionSerializer
from .views import _prescreener_questions


class RFGGeoRequirementDisplayTests(TestCase):
    def setUp(self):
        client = Client.objects.create(
            code="rfg-geo-display",
            name="RFG Geo Display",
            provider_code="rfg",
        )
        self.integration = ClientIntegration.objects.create(
            client=client,
            name="RFG Geo Display",
            provider_code="rfg",
            base_url="https://api.researchforgood.com/API/",
            credential_env_keys={"apid": "RFG_APID", "secret": "RFG_SECRET"},
            sync_interval_seconds=60,
        )
        self.survey = Survey.objects.create(
            client=client,
            integration=self.integration,
            source_key="RFG605150-geo-display",
            country_code="US",
            status=Survey.Status.LIVE,
        )

    @patch.dict(
        "os.environ",
        {
            "RFG_APID": "publisher",
            "RFG_SECRET": "00112233445566778899aabbccddeeff",
        },
        clear=False,
    )
    def test_refresh_puts_required_dma_and_zip_values_in_postal_question(self):
        provider = ResearchForGoodProvider(self.integration)
        targeting = {
            "datapoints": [
                {"name": "DMA (US)", "values": [{"choice": 1}, {"choice": 2}]},
                {
                    "name": "List of Zips",
                    "values": [{"freelist": '"10001",90001,981*'}],
                    "usesWildcards": True,
                },
            ],
            "quotas": [],
        }
        metadata = {
            "DMA (US)": {
                "name": "DMA (US)",
                "type": 13,
                "answers": [
                    None,
                    {"en-US": "LOS ANGELES"},
                    {"en-US": "NEW YORK"},
                ],
            },
            "List of Zips": {
                "name": "List of Zips",
                "type": 16,
                "answers": [],
            },
        }
        with patch.object(provider, "targeting", return_value=targeting), patch.object(
            provider, "datapoint", side_effect=lambda name: metadata[name]
        ), patch.object(
            provider,
            "create_link",
            return_value="https://survey.saysoforgood.com/live/example",
        ):
            provider.refresh_details(self.survey)

        postal = self.survey.targeting_questions.get(key="RFG_POSTAL_CODE")
        expected_note = (
            "Required DMA: LOS ANGELES, NEW YORK · "
            "Required ZIP codes/patterns: 10001, 90001, 981*"
        )
        self.assertEqual(postal.raw_data["targeting_note"], expected_note)
        self.assertEqual(
            postal.raw_data["targeting_requirements"][0]["values"],
            ["LOS ANGELES", "NEW YORK"],
        )
        self.assertEqual(
            postal.text,
            "What is your postal code?",
        )
        self.assertEqual(
            TargetingQuestionSerializer(postal).data["targeting_note"],
            expected_note,
        )
        prepared_postal = next(
            item for item in _prescreener_questions(self.survey)
            if item["model"].key == "RFG_POSTAL_CODE"
        )
        self.assertEqual(prepared_postal["display_text"], "What is your postal code?")
        self.assertEqual(prepared_postal["targeting_note"], expected_note)

    @patch.dict(
        "os.environ",
        {
            "RFG_APID": "publisher",
            "RFG_SECRET": "00112233445566778899aabbccddeeff",
        },
        clear=False,
    )
    def test_open_quota_region_is_shown_in_postal_hint(self):
        provider = ResearchForGoodProvider(self.integration)
        targeting = {
            "datapoints": [
                {"name": "Age", "values": [{"min": 18, "max": 99}]},
            ],
            "quotas": [
                {
                    "completesLeft": 27,
                    "datapoints": [
                        {"name": "Region1GB", "values": [{"choice": 1}]},
                        {"name": "Age", "values": [{"min": 18, "max": 99}]},
                    ],
                },
                {
                    "completesLeft": 0,
                    "datapoints": [
                        {"name": "Region1GB", "values": [{"choice": 2}]},
                    ],
                },
            ],
        }
        metadata = {
            "Age": {
                "name": "Age",
                "type": 15,
                "answers": [],
            },
            "Region1GB": {
                "name": "Region1GB",
                "type": 13,
                "question": {
                    "en-US": (
                        "In which Region / District Council area / "
                        "Principal area do you live?"
                    ),
                },
                "answers": [
                    None,
                    {"en-US": "Derry and Strabane"},
                    {"en-US": "Belfast"},
                ],
            },
        }
        with patch.object(provider, "targeting", return_value=targeting), patch.object(
            provider, "datapoint", side_effect=lambda name: metadata[name]
        ), patch.object(
            provider,
            "create_link",
            return_value="https://survey.saysoforgood.com/live/example",
        ):
            provider.refresh_details(self.survey)

        postal = self.survey.targeting_questions.get(key="RFG_POSTAL_CODE")
        expected_note = "Open quota region: Derry and Strabane"
        self.assertEqual(postal.raw_data["targeting_note"], expected_note)
        self.assertNotIn("Belfast", postal.raw_data["targeting_note"])
        self.assertEqual(
            postal.raw_data["targeting_requirements"],
            [{
                "name": "Region1GB",
                "label": "Open quota region",
                "values": ["Derry and Strabane"],
                "uses_wildcards": False,
                "scope": "quota",
            }],
        )
        prepared_postal = next(
            item for item in _prescreener_questions(self.survey)
            if item["model"].key == "RFG_POSTAL_CODE"
        )
        self.assertEqual(prepared_postal["targeting_note"], expected_note)

        # A cached survey whose provider endpoint is no longer available can
        # rebuild the same hint from its already-normalized quota details.
        postal.text = "What is your postal code?"
        postal.raw_data = {
            key: value for key, value in postal.raw_data.items()
            if key not in {"targeting_note", "targeting_requirements"}
        }
        postal.save(update_fields=["text", "raw_data", "updated_at"])
        call_command(
            "backfill_rfg_geo_hints",
            survey=self.survey.source_key,
            verbosity=0,
        )
        postal.refresh_from_db()
        self.assertEqual(postal.raw_data["targeting_note"], expected_note)
        self.assertEqual(postal.text, "What is your postal code?")
        self.assertNotIn("Belfast", postal.raw_data["targeting_note"])
