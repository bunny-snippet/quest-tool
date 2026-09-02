"""Survey pages, public respondent routes and inventory/report REST router."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    CanonicalQuestionViewSet,
    DashboardAPIView,
    ProviderQuestionMappingViewSet,
    SurveyAttemptViewSet,
    UserHitsAPIView,
    SyncRunViewSet,
    SyncTriggerView,
    SurveyViewSet,
    dashboard_page,
    export_job_create,
    export_job_download,
    export_job_status,
    projects_page,
    biobrain_data_page,
    studies_page,
    prescreener_data_page,
    prescreener_data_export,
    termination_reasons_page,
    termination_reasons_export,
    user_hits_page,
    survey_start,
    survey_security_check,
    RFGCallbackAPIView,
    rfg_result,
    survey_status,
    workspace_home,
)
from .webhook_views import cint_opportunities_webhook

router = DefaultRouter()
router.register("surveys", SurveyViewSet, basename="survey")
router.register("canonical-questions", CanonicalQuestionViewSet, basename="canonical-question")
router.register("provider-question-mappings", ProviderQuestionMappingViewSet, basename="provider-question-mapping")
router.register("sync-runs", SyncRunViewSet, basename="sync-run")
router.register("survey-attempts", SurveyAttemptViewSet, basename="survey-attempt")

urlpatterns = [
    path("api/cint/webhook/surveys", cint_opportunities_webhook, name="cint-opportunities-webhook"),
    path("survey/start", survey_start, name="survey-start"),
    path("survey/security-check", survey_security_check, name="survey-security-check"),
    path("survey/rfg/callback", RFGCallbackAPIView.as_view(), name="rfg-callback"),
    path("survey/rfg/result", rfg_result, name="rfg-result"),
    path("survey", survey_status, name="survey-status"),
    path("", workspace_home, name="home"),
    path("dashboard/", dashboard_page, name="dashboard"),
    path("projects/", projects_page, name="projects"),
    path("studies/", studies_page, name="studies"),
    path("traffic-reports/", studies_page, name="traffic-reports"),
    path("prescreened-data/", prescreener_data_page, name="prescreened-data"),
    path("prescreened-data/export/", prescreener_data_export, name="prescreened-data-export"),
    path("biobrain-data/", biobrain_data_page, name="biobrain-data"),
    path("termination-reasons/", termination_reasons_page, name="termination-reasons"),
    path("termination-reasons/export/", termination_reasons_export, name="termination-reasons-export"),
    path("user-hits/", user_hits_page, name="user-hits"),
    path("api/v1/export-jobs/<str:kind>/", export_job_create, name="export-job-create"),
    path("api/v1/export-jobs/status/<uuid:public_id>/", export_job_status, name="export-job-status"),
    path("api/v1/export-jobs/download/<uuid:public_id>/", export_job_download, name="export-job-download"),
    path("api/v1/dashboard/", DashboardAPIView.as_view(), name="dashboard-api"),
    path("api/v1/user-hits/", UserHitsAPIView.as_view(), name="user-hits-api"),
    path("api/v1/sync/", SyncTriggerView.as_view(), name="sync-trigger"),
    path("api/v1/", include(router.urls)),
]
