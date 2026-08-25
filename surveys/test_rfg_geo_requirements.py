from unittest.mock import patch

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
            f"What is your postal code? {expected_note}",
        )
        self.assertIn(
            expected_note,
            TargetingQuestionSerializer(postal).data["text"],
        )
        prepared_postal = next(
            item for item in _prescreener_questions(self.survey)
            if item["model"].key == "RFG_POSTAL_CODE"
        )
        self.assertIn(expected_note, prepared_postal["display_text"])
