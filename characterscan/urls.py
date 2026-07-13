"""App URLs"""

# Django
from django.urls import path

# AA CharacterScan App
from characterscan import views

app_name: str = "characterscan"  # pylint: disable=invalid-name

urlpatterns = [
    path("", views.index, name="index"),
    path("apply/", views.apply, name="apply"),
    path("recruits/", views.recruiter_list, name="recruiter_list"),
    path("log/", views.activity_log, name="activity_log"),
    path("grant-standings/", views.grant_standings, name="grant_standings"),
    path("recruits/<int:pk>/", views.recruit_detail, name="recruit_detail"),
    path("recruits/<int:pk>/action/", views.recruit_action, name="recruit_action"),
    path("recruits/<int:pk>/blacklist/", views.blacklist_recruit, name="blacklist_recruit"),
]
