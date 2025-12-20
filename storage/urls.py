"""
URL configuration for storage project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import include, path

from .views import (
    bin_page,
    dashboard,
    delete_file,
    confirm_restore_backup,
    database_backup_list,
    download_file,
    logout_view,
    play_video,
    purge_file,
    restore_file,
    rename_file,
    stream_segment,
    upload_file,
    video_library,
    video_page,
)

urlpatterns = [
    path('', dashboard, name='dashboard'),
    path('dashboard/', dashboard, name='dashboard_legacy'),
    path('admin/', admin.site.urls),
    path('backups/', database_backup_list, name='database_backup_list'),
    path('backups/restore/', confirm_restore_backup, name='confirm_restore_backup'),
    path('upload/', upload_file, name='upload'),
    path('video/', video_page, name='video'),
    path('bin/', bin_page, name='bin'),
    path('videos/', video_library, name='video_library'),
    path(
        'videos/<uuid:file_uuid>/segments/<path:segment>',
        stream_segment,
        name='stream_segment',
    ),
    path('files/<int:file_id>/download/', download_file, name='download_file'),
    path('files/<int:file_id>/play/', play_video, name='play_video'),
    path('files/<int:file_id>/rename/', rename_file, name='rename_file'),
    path('files/<int:file_id>/delete/', delete_file, name='delete_file'),
    path('bin/files/<int:file_id>/purge/', purge_file, name='purge_file'),
    path('bin/files/<int:file_id>/restore/', restore_file, name='restore_file'),
    # Override logout to allow safe GET-based logout with ?next=
    path('accounts/logout/', logout_view, name='logout'),
    path('accounts/', include('django.contrib.auth.urls')),  # login, logout, password reset
]
