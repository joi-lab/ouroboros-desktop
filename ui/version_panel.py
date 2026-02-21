"""Version management panel for the Dashboard."""

import logging
import threading

import flet as ft

log = logging.getLogger(__name__)


def _build_tag_row(tag_info: dict, on_rollback):
    tag_name = tag_info["tag"]
    date_str = tag_info["date"][:10] if tag_info.get("date") else ""
    msg = tag_info["message"][:60] if tag_info.get("message") else ""
    return ft.Container(
        padding=ft.padding.symmetric(horizontal=10, vertical=6),
        border_radius=6,
        bgcolor=ft.Colors.with_opacity(0.05, ft.Colors.WHITE),
        content=ft.Row([
            ft.Container(
                padding=ft.padding.symmetric(horizontal=6, vertical=2),
                border_radius=4, bgcolor=ft.Colors.TEAL_900,
                content=ft.Text(tag_name, size=12, weight=ft.FontWeight.BOLD,
                                color=ft.Colors.TEAL_200),
            ),
            ft.Text(date_str, size=11, color=ft.Colors.WHITE38),
            ft.Text(msg, size=11, color=ft.Colors.WHITE54, expand=True),
            ft.IconButton(
                icon=ft.Icons.RESTORE, icon_size=16,
                tooltip=f"Rollback to {tag_name}",
                icon_color=ft.Colors.AMBER_300,
                on_click=lambda _e, tg=tag_name: on_rollback(tg),
            ),
        ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
    )


def _build_commit_row(commit_info: dict, on_rollback):
    date_str = commit_info["date"][:10] if commit_info.get("date") else ""
    return ft.Container(
        padding=ft.padding.symmetric(horizontal=10, vertical=4),
        border_radius=4,
        content=ft.Row([
            ft.Text(commit_info["short_sha"], size=11, font_family="monospace",
                    color=ft.Colors.AMBER_200, width=60),
            ft.Text(date_str, size=11, color=ft.Colors.WHITE38),
            ft.Text(commit_info["message"][:50], size=11,
                    color=ft.Colors.WHITE54, expand=True),
            ft.IconButton(
                icon=ft.Icons.RESTORE, icon_size=14,
                tooltip=f"Rollback to {commit_info['short_sha']}",
                icon_color=ft.Colors.WHITE24,
                on_click=lambda _e, sha=commit_info["sha"]: on_rollback(sha),
            ),
        ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
    )


def create_version_panel(page: ft.Page, settings: dict, repo_dir):
    """Build version management controls. Returns (container, refresh_fn)."""
    list_view = ft.ListView(spacing=2, padding=8, height=280)
    status_text = ft.Text("", size=12, color=ft.Colors.WHITE54)

    def _do_rollback_bg(target: str):
        try:
            from supervisor.git_ops import rollback_to_version
            ok, msg = rollback_to_version(target, reason="manual_ui_rollback")
            if ok:
                load_versions()
                page.open(ft.SnackBar(ft.Text(f"Rollback OK: {msg}"), duration=4000))
            else:
                page.open(ft.SnackBar(ft.Text(f"Rollback failed: {msg}"), duration=4000))
        except Exception as exc:
            page.open(ft.SnackBar(ft.Text(f"Rollback error: {exc}"), duration=4000))
        try:
            page.update()
        except Exception:
            pass

    def _on_rollback(target: str):
        def _close(_e2): dlg.open = False; page.update()
        def _confirm(_e2):
            dlg.open = False; page.update()
            threading.Thread(target=_do_rollback_bg, args=(target,), daemon=True).start()
        short = target[:12] if len(target) > 12 else target
        dlg = ft.AlertDialog(
            modal=True, title=ft.Text("Confirm Rollback"),
            content=ft.Text(
                f"Roll back to {short}?\n\n"
                "A rescue snapshot of the current state will be saved."
            ),
            actions=[
                ft.TextButton("Cancel", on_click=_close),
                ft.TextButton("Rollback", on_click=_confirm,
                              style=ft.ButtonStyle(color=ft.Colors.AMBER_400)),
            ],
        )
        page.open(dlg); page.update()

    def load_versions():
        list_view.controls.clear()
        try:
            from supervisor.git_ops import list_versions, list_commits
            if not (repo_dir / ".git").exists():
                status_text.value = "Repository not initialized yet."
                return
            tags = list_versions(max_count=20)
            commits = list_commits(max_count=20)

            if tags:
                list_view.controls.append(
                    ft.Text("Tagged Versions", size=12, weight=ft.FontWeight.BOLD,
                            color=ft.Colors.TEAL_200))
                for t in tags:
                    list_view.controls.append(_build_tag_row(t, _on_rollback))
            if commits:
                list_view.controls.append(ft.Container(height=6))
                list_view.controls.append(
                    ft.Text("Recent Commits", size=12, weight=ft.FontWeight.BOLD,
                            color=ft.Colors.WHITE54))
                for c in commits[:15]:
                    list_view.controls.append(_build_commit_row(c, _on_rollback))
            if not tags and not commits:
                status_text.value = "No versions or commits found."
            else:
                status_text.value = f"{len(tags)} tags, {len(commits)} recent commits"
        except Exception as exc:
            status_text.value = f"Error loading versions: {exc}"

    def _on_push(_e):
        def _do():
            try:
                from supervisor.git_ops import configure_remote, push_to_remote
                token = settings.get("GITHUB_TOKEN", "")
                slug = settings.get("GITHUB_REPO", "")
                if not token or not slug:
                    page.open(ft.SnackBar(ft.Text("Configure GitHub token and repo in Settings first."), duration=4000))
                else:
                    ok_r, msg_r = configure_remote(slug, token)
                    if not ok_r:
                        page.open(ft.SnackBar(ft.Text(f"Remote failed: {msg_r}"), duration=4000))
                    else:
                        ok_p, msg_p = push_to_remote()
                        snack = f"Push OK: {msg_p}" if ok_p else f"Push failed: {msg_p}"
                        page.open(ft.SnackBar(ft.Text(snack), duration=3000))
            except Exception as exc:
                page.open(ft.SnackBar(ft.Text(f"Push error: {exc}"), duration=4000))
            try:
                page.update()
            except Exception:
                pass
        threading.Thread(target=_do, daemon=True).start()

    load_versions()

    header = ft.Row([
        ft.Text("Version History", size=14, weight=ft.FontWeight.BOLD,
                color=ft.Colors.WHITE54),
        ft.Container(expand=True),
        status_text,
        ft.IconButton(icon=ft.Icons.REFRESH, icon_size=16, tooltip="Refresh",
                      on_click=lambda _e: (load_versions(), page.update())),
        ft.ElevatedButton("Push to GitHub", icon=ft.Icons.CLOUD_UPLOAD_OUTLINED,
                          on_click=_on_push),
    ], vertical_alignment=ft.CrossAxisAlignment.CENTER)

    container = ft.Column(spacing=8, controls=[
        header,
        ft.Container(
            border=ft.border.all(0.5, ft.Colors.WHITE10),
            border_radius=8,
            bgcolor=ft.Colors.with_opacity(0.03, ft.Colors.WHITE),
            content=list_view,
        ),
    ])
    return container, load_versions
