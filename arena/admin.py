"""管理員後台（/admin/）：登入紀錄與目前在線的訪客（使用者要求，2026-11-05），以及對局。"""
from django.contrib import admin
from django.utils import timezone

from .models import Game, LoginRecord, Presence


@admin.register(LoginRecord)
class LoginRecordAdmin(admin.ModelAdmin):
    list_display = ["created_at", "user", "ip", "user_agent"]
    list_filter = ["user"]
    search_fields = ["user__username", "ip"]
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False  # 紀錄只由登入時寫入


class OnlineFilter(admin.SimpleListFilter):
    """預設只列目前在線（Presence.ONLINE 內有活動）的訪客。"""
    title = "狀態"
    parameter_name = "status"

    def lookups(self, request, model_admin):
        minutes = int(Presence.ONLINE.total_seconds() // 60)
        return [("online", f"目前在線（{minutes} 分鐘內有活動）"), ("all", "全部")]

    def choices(self, changelist):
        current = self.value() or "online"
        for value, title in self.lookup_choices:
            yield {"selected": current == value, "query_string": changelist.get_query_string({self.parameter_name: value}),
                   "display": title}

    def queryset(self, request, queryset):
        if self.value() == "all":
            return queryset
        return queryset.filter(last_seen__gte=timezone.now() - Presence.ONLINE)


@admin.register(Presence)
class PresenceAdmin(admin.ModelAdmin):
    list_display = ["last_seen", "who", "ip", "path", "first_seen", "user_agent"]
    list_filter = [OnlineFilter]
    search_fields = ["user__username", "ip", "path"]

    @admin.display(description="帳號", ordering="user__username")
    def who(self, obj):
        return obj.user.username if obj.user else "（未登入）"

    def has_add_permission(self, request):
        return False


@admin.register(Game)
class GameAdmin(admin.ModelAdmin):
    list_display = ["id", "game_type", "owner", "human_seat", "winner", "draw", "ended", "updated_at"]
    list_filter = ["game_type"]
    search_fields = ["owner__username"]
    exclude = ["secret"]  # 撲克的洗牌種子，後台也不顯示
