from django.urls import path

from . import views

app_name = "arena"

urlpatterns = [
    path("", views.home, name="home"),
    # 要放在 <slug:game_key>/ 前面，否則會被當成遊戲名稱
    path("history/", views.history, name="history"),
    path("guide/", views.guide, name="guide"),
    path("join/", views.join, name="join"),
    path("join/<str:code>/", views.join, name="join_code"),
    path("games/<int:game_id>/end/", views.end_game, name="end_game"),
    path("games/<int:game_id>/close/", views.close_room, name="close_room"),
    path("api/<slug:game_key>/models/", views.api_models, name="models"),
    path("api/<slug:game_key>/games/", views.api_games, name="games"),
    path("api/<slug:game_key>/games/<int:game_id>/", views.api_game_detail, name="game_detail"),
    path("api/<slug:game_key>/games/<int:game_id>/move/", views.api_game_move, name="game_move"),
    path("api/<slug:game_key>/games/<int:game_id>/end/", views.api_game_end, name="game_end"),
    path("api/<slug:game_key>/games/<int:game_id>/room/", views.api_room_settings, name="room_settings"),
    path("api/<slug:game_key>/games/<int:game_id>/start/", views.api_room_start, name="room_start"),
    path("api/<slug:game_key>/games/<int:game_id>/chat/", views.api_room_chat, name="room_chat"),
    path("api/<slug:game_key>/games/<int:game_id>/close/", views.api_room_close, name="room_close"),
    path("api/<slug:game_key>/games/<int:game_id>/review/", views.api_game_review, name="game_review"),
    path("api/<slug:game_key>/games/<int:game_id>/ask/", views.api_game_ask, name="game_ask"),
    path("api/<slug:game_key>/games/<int:game_id>/notes/<int:index>/narrate/", views.api_note_narrate,
         name="note_narrate"),
    path("api/<slug:game_key>/narrate/", views.api_narrate, name="narrate"),
    path("<slug:game_key>/", views.play, name="play"),
]
