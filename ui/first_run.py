"""First-run setup wizard."""

import logging
import threading

import flet as ft

log = logging.getLogger(__name__)

SUGGESTED_MODELS = {
    "main": [
        "anthropic/claude-opus-4.6",
        "anthropic/claude-sonnet-4.6",
        "google/gemini-3.1-pro-preview",
        "openai/gpt-5.2",
    ],
    "code": [
        "anthropic/claude-opus-4.6",
        "anthropic/claude-sonnet-4.6",
        "google/gemini-3.1-pro-preview",
        "openai/gpt-5.2",
    ],
    "light": [
        "anthropic/claude-opus-4.6",
        "anthropic/claude-sonnet-4.6",
        "google/gemini-3.1-pro-preview",
        "openai/gpt-5.2",
        "google/gemini-3-flash-preview",
    ],
    "fallback": [
        "google/gemini-2.5-flash",
        "google/gemini-3-flash-preview",
        "anthropic/claude-sonnet-4",
    ],
}

_FIELD_WIDTH = 440


def _make_model_row(field: ft.TextField, suggestions: list, page_ref: list):
    """Create a model field with clickable suggestion chips."""
    def _pick(val):
        field.value = val
        if page_ref[0]:
            page_ref[0].update()

    chips = ft.Row(
        controls=[
            ft.OutlinedButton(
                m.split("/")[-1],
                on_click=lambda _e, mv=m: _pick(mv),
                style=ft.ButtonStyle(
                    padding=ft.padding.symmetric(horizontal=8, vertical=0),
                    side=ft.BorderSide(0.5, ft.Colors.WHITE24),
                    shape=ft.RoundedRectangleBorder(radius=6),
                ),
                height=28,
            )
            for m in suggestions
        ],
        wrap=True, spacing=4, run_spacing=4,
    )
    return ft.Column(spacing=2, controls=[field, chips])


def _test_api_key(key: str, status_text: ft.Text, page: ft.Page):
    """Test OpenRouter API key in background thread."""
    try:
        import requests
        r = requests.get(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=10,
        )
        if r.status_code == 200:
            status_text.value = "Connection successful!"
            status_text.color = ft.Colors.GREEN_300
        else:
            status_text.value = f"API returned {r.status_code}. Check your key."
            status_text.color = ft.Colors.RED_300
    except Exception as exc:
        status_text.value = f"Connection failed: {exc}"
        status_text.color = ft.Colors.RED_300
    page.update()


def _build_step_models(page_ref, fields, go_step):
    """Build Step 2: model selection."""
    return ft.Column(
        visible=False, spacing=10,
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        scroll=ft.ScrollMode.AUTO,
        controls=[
            ft.Text("Step 2: Choose Models", size=20, weight=ft.FontWeight.BOLD),
            ft.Text("Pick or type model names. You can change these later in Settings.",
                    size=13, color=ft.Colors.WHITE70, text_align=ft.TextAlign.CENTER),
            _make_model_row(fields["main"], SUGGESTED_MODELS["main"], page_ref),
            _make_model_row(fields["code"], SUGGESTED_MODELS["code"], page_ref),
            _make_model_row(fields["light"], SUGGESTED_MODELS["light"], page_ref),
            ft.Divider(height=1, color=ft.Colors.WHITE10),
            _make_model_row(fields["fallback"], SUGGESTED_MODELS["fallback"], page_ref),
            ft.Container(height=4),
            ft.Row([
                ft.TextButton("Back", on_click=lambda _: go_step(1)),
                ft.FilledButton("Next", on_click=lambda _: go_step(3)),
            ], alignment=ft.MainAxisAlignment.CENTER),
        ],
    )


def _build_step_github(github_fields, go_step, on_finish):
    """Build Step 3: GitHub configuration."""
    return ft.Column(
        visible=False, spacing=14,
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        controls=[
            ft.Text("Step 3: GitHub (optional)", size=20, weight=ft.FontWeight.BOLD),
            ft.Text(
                "Connect a GitHub repo to store Ouroboros versions remotely.\n"
                "You can skip this and configure later in Settings.",
                size=13, color=ft.Colors.WHITE70, text_align=ft.TextAlign.CENTER),
            github_fields["token"],
            github_fields["repo"],
            ft.Text("Token needs 'repo' scope. Repo format: owner/repo-name",
                    size=11, color=ft.Colors.WHITE38),
            ft.Container(height=8),
            ft.Row([
                ft.TextButton("Back", on_click=lambda _: go_step(2)),
                ft.OutlinedButton("Skip", on_click=on_finish),
                ft.FilledButton("Launch Ouroboros", on_click=on_finish,
                                icon=ft.Icons.ROCKET_LAUNCH),
            ], alignment=ft.MainAxisAlignment.CENTER),
        ],
    )


def run_first_run_wizard(
    settings_defaults: dict, save_fn,
    assets_dir: str = "assets",
) -> bool:
    """Show a setup wizard. Returns True if user completed setup."""
    _completed = [False]

    def _wizard(page: ft.Page):
        page.title = "Ouroboros \u2014 Setup"
        page.theme_mode = ft.ThemeMode.DARK
        page.window.width = 600
        page.window.height = 720
        page.padding = 0
        page.spacing = 0

        page_ref = [page]
        step = [0]
        status_text = ft.Text("", size=13)

        api_keys = {
            "openrouter": ft.TextField(label="OpenRouter API Key", password=True,
                                       can_reveal_password=True, width=_FIELD_WIDTH, hint_text="sk-or-..."),
            "openai": ft.TextField(label="OpenAI API Key (for web search)", password=True,
                                   can_reveal_password=True, width=_FIELD_WIDTH, hint_text="sk-... (optional)"),
            "anthropic": ft.TextField(label="Anthropic API Key", password=True,
                                      can_reveal_password=True, width=_FIELD_WIDTH, hint_text="sk-ant-... (optional)"),
        }
        model_fields = {
            "main": ft.TextField(label="Main Model (reasoning, chat)", width=_FIELD_WIDTH,
                                 value="anthropic/claude-sonnet-4.6"),
            "code": ft.TextField(label="Code Model (editing, commits)", width=_FIELD_WIDTH,
                                 value="anthropic/claude-sonnet-4.6"),
            "light": ft.TextField(label="Light Model (dedup, safety checks)", width=_FIELD_WIDTH,
                                  value="google/gemini-2.5-flash"),
            "fallback": ft.TextField(label="Fallback Model (when others are down)", width=_FIELD_WIDTH,
                                     value="google/gemini-2.5-flash"),
        }
        github_fields = {
            "token": ft.TextField(label="GitHub Token (optional)", password=True,
                                  can_reveal_password=True, width=_FIELD_WIDTH, hint_text="ghp_... or github_pat_..."),
            "repo": ft.TextField(label="GitHub Repo (optional)", width=_FIELD_WIDTH,
                                 hint_text="owner/repo-name"),
        }

        def _go_step(n):
            step[0] = n
            for i, s in enumerate(step_views):
                s.visible = (i == n)
            page.update()

        def _on_test_key(_e):
            key = api_keys["openrouter"].value.strip()
            if not key:
                status_text.value = "Please enter an API key."
                status_text.color = ft.Colors.RED_300
                page.update()
                return
            status_text.value = "Testing connection..."
            status_text.color = ft.Colors.AMBER_300
            page.update()
            threading.Thread(target=_test_api_key, args=(key, status_text, page), daemon=True).start()

        def _on_finish(_e):
            try:
                s = dict(settings_defaults)
                s["OPENROUTER_API_KEY"] = api_keys["openrouter"].value.strip()
                s["OPENAI_API_KEY"] = api_keys["openai"].value.strip()
                s["ANTHROPIC_API_KEY"] = api_keys["anthropic"].value.strip()
                s["OUROBOROS_MODEL"] = model_fields["main"].value.strip()
                s["OUROBOROS_MODEL_CODE"] = model_fields["code"].value.strip()
                s["OUROBOROS_MODEL_LIGHT"] = model_fields["light"].value.strip()
                s["OUROBOROS_MODEL_FALLBACK"] = model_fields["fallback"].value.strip()
                s["GITHUB_TOKEN"] = github_fields["token"].value.strip()
                s["GITHUB_REPO"] = github_fields["repo"].value.strip()
                save_fn(s)
                _completed[0] = True
            except Exception as exc:
                log.error("Wizard save failed: %s", exc, exc_info=True)
            page.window.destroy()

        step0 = ft.Column(
            visible=True, spacing=20,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Container(
                    width=80, height=80, border_radius=18,
                    clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                    content=ft.Image(src="logo.jpg", width=80, height=80, fit=ft.ImageFit.COVER),
                ),
                ft.Text("Welcome to Ouroboros", size=24, weight=ft.FontWeight.BOLD),
                ft.Text("A self-creating agent running locally on your Mac.\n"
                        "Let\u2019s get you set up in a few steps.",
                        size=14, color=ft.Colors.WHITE70, text_align=ft.TextAlign.CENTER),
                ft.FilledButton("Get Started", on_click=lambda _: _go_step(1)),
            ],
        )
        step1 = ft.Column(
            visible=False, spacing=14,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Text("Step 1: API Keys", size=20, weight=ft.FontWeight.BOLD),
                ft.Text("Ouroboros uses OpenRouter for LLM access.\nGet a key at openrouter.ai",
                        size=13, color=ft.Colors.WHITE70, text_align=ft.TextAlign.CENTER),
                api_keys["openrouter"],
                ft.OutlinedButton("Test Connection", on_click=_on_test_key),
                status_text,
                api_keys["openai"], api_keys["anthropic"],
                ft.Text("OpenAI key enables web search. Anthropic key is optional.",
                         size=11, color=ft.Colors.WHITE38),
                ft.Row([ft.TextButton("Back", on_click=lambda _: _go_step(0)),
                        ft.FilledButton("Next", on_click=lambda _: _go_step(2))],
                       alignment=ft.MainAxisAlignment.CENTER),
            ],
        )
        step2 = _build_step_models(page_ref, model_fields, _go_step)
        step3 = _build_step_github(github_fields, _go_step, _on_finish)

        step_views = [step0, step1, step2, step3]
        page.add(ft.Container(
            expand=True, padding=ft.padding.symmetric(horizontal=30, vertical=20),
            alignment=ft.alignment.center,
            content=ft.Stack(controls=step_views),
        ))

    ft.app(target=_wizard, assets_dir=assets_dir)
    return _completed[0]
