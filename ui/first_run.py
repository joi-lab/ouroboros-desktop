"""First-run setup wizard."""

import threading
from typing import Dict

import flet as ft


def run_first_run_wizard(
    models: list, settings_defaults: dict, save_fn,
) -> bool:
    """Show a setup wizard. Returns True if user completed setup."""
    _completed = [False]

    def _wizard(page: ft.Page):
        page.title = "Ouroboros \u2014 Setup"
        page.theme_mode = ft.ThemeMode.DARK
        page.window.width = 600
        page.window.height = 520
        page.padding = 0
        page.spacing = 0

        step = [0]
        status_text = ft.Text("", size=13)

        api_key_input = ft.TextField(
            label="OpenRouter API Key", password=True, can_reveal_password=True,
            width=450, hint_text="sk-or-...",
        )
        openai_key_input = ft.TextField(
            label="OpenAI API Key (for web search)", password=True,
            can_reveal_password=True, width=450, hint_text="sk-... (optional)",
        )
        model_dropdown = ft.Dropdown(
            label="Main Model", width=450,
            options=[ft.dropdown.Option(m) for m in models],
            value=models[0] if models else "",
        )

        def _go_step(n):
            step[0] = n
            for i, s in enumerate(step_views):
                s.visible = (i == n)
            page.update()

        def _on_test_key(_e):
            key = api_key_input.value.strip()
            if not key:
                status_text.value = "Please enter an API key."
                status_text.color = ft.Colors.RED_300
                page.update()
                return
            status_text.value = "Testing connection..."
            status_text.color = ft.Colors.AMBER_300
            page.update()

            def _test():
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

            threading.Thread(target=_test, daemon=True).start()

        def _on_finish(_e):
            s = dict(settings_defaults)
            s["OPENROUTER_API_KEY"] = api_key_input.value.strip()
            s["OPENAI_API_KEY"] = openai_key_input.value.strip()
            s["OUROBOROS_MODEL"] = model_dropdown.value
            s["OUROBOROS_MODEL_CODE"] = model_dropdown.value
            save_fn(s)
            _completed[0] = True
            page.window.close()

        step0 = ft.Column(
            visible=True, spacing=20,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Text("O", size=64, weight=ft.FontWeight.BOLD, color=ft.Colors.TEAL_200),
                ft.Text("Welcome to Ouroboros", size=24, weight=ft.FontWeight.BOLD),
                ft.Text(
                    "A self-creating agent running locally on your Mac.\n"
                    "Let\u2019s get you set up in a few steps.",
                    size=14, color=ft.Colors.WHITE70, text_align=ft.TextAlign.CENTER,
                ),
                ft.FilledButton("Get Started", on_click=lambda _: _go_step(1)),
            ],
        )

        step1 = ft.Column(
            visible=False, spacing=16,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Text("Step 1: API Keys", size=20, weight=ft.FontWeight.BOLD),
                ft.Text(
                    "Ouroboros needs an OpenRouter API key to communicate with LLMs.\n"
                    "Get one at openrouter.ai",
                    size=13, color=ft.Colors.WHITE70, text_align=ft.TextAlign.CENTER,
                ),
                api_key_input,
                ft.OutlinedButton("Test Connection", on_click=_on_test_key),
                status_text,
                openai_key_input,
                ft.Text("OpenAI key enables web search (optional).", size=11, color=ft.Colors.WHITE38),
                ft.Row([
                    ft.TextButton("Back", on_click=lambda _: _go_step(0)),
                    ft.FilledButton("Next", on_click=lambda _: _go_step(2)),
                ], alignment=ft.MainAxisAlignment.CENTER),
            ],
        )

        step2 = ft.Column(
            visible=False, spacing=16,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Text("Step 2: Choose a Model", size=20, weight=ft.FontWeight.BOLD),
                ft.Text(
                    "Select the default model. You can change this later in Settings.",
                    size=13, color=ft.Colors.WHITE70,
                ),
                model_dropdown,
                ft.Row([
                    ft.TextButton("Back", on_click=lambda _: _go_step(1)),
                    ft.FilledButton("Launch Ouroboros", on_click=_on_finish, icon=ft.Icons.ROCKET_LAUNCH),
                ], alignment=ft.MainAxisAlignment.CENTER),
            ],
        )

        step_views = [step0, step1, step2]
        page.add(ft.Container(
            expand=True, padding=40,
            alignment=ft.alignment.center,
            content=ft.Stack(controls=step_views),
        ))

    ft.app(target=_wizard)
    return _completed[0]
