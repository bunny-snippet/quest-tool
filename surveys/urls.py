from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    SurveyAttemptViewSet,
    SyncRunViewSet,
    SyncTriggerView,
    SurveyViewSet,
    dashboard_page,
    projects_page,
    studies_page,
    survey_start,
    survey_status,
    workspace_home,
)

router = DefaultRouter()
router.register("surveys", SurveyViewSet, basename="survey")
router.register("sync-runs", SyncRunViewSet, basename="sync-run")
router.register("survey-attempts", SurveyAttemptViewSet, basename="survey-attempt")

urlpatterns = [
    path("survey/start", survey_start, name="survey-start"),
    path("survey", survey_status, name="survey-status"),
    path("", workspace_home, name="home"),
    path("dashboard/", dashboard_page, name="dashboard"),
    path("projects/", projects_page, name="projects"),
    path("studies/", studies_page, name="studies"),
    path("api/v1/sync/", SyncTriggerView.as_view(), name="sync-trigger"),
    path("api/v1/", include(router.urls)),
]
