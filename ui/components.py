"""Reusable Flet UI components."""

import flet as ft


class ChatBubble(ft.Container):
    def __init__(self, text: str, is_user: bool, markdown: bool = False):
        super().__init__()
        self.padding = ft.padding.symmetric(horizontal=14, vertical=10)
        self.border_radius = ft.border_radius.all(16)
        self.margin = ft.margin.only(
            left=120 if is_user else 0,
            right=0 if is_user else 120,
            top=2, bottom=2,
        )
        self.bgcolor = (
            ft.Colors.BLUE_700 if is_user
            else ft.Colors.with_opacity(0.12, ft.Colors.WHITE)
        )

        label = "You" if is_user else "Ouroboros"
        label_color = ft.Colors.BLUE_200 if is_user else ft.Colors.TEAL_200

        msg_control = (
            ft.Markdown(text, selectable=True) if markdown
            else ft.Text(
                text, size=14, selectable=True,
                color=ft.Colors.WHITE if is_user else ft.Colors.WHITE70,
            )
        )

        self.content = ft.Column(
            spacing=4,
            controls=[
                ft.Text(label, size=11, weight=ft.FontWeight.BOLD, color=label_color),
                msg_control,
            ],
        )


def status_card(
    title: str, value_control: ft.Control,
    icon: str, icon_color: str = ft.Colors.TEAL_200,
) -> ft.Container:
    return ft.Container(
        bgcolor=ft.Colors.with_opacity(0.06, ft.Colors.WHITE),
        border_radius=12, padding=20, expand=True,
        content=ft.Column(
            spacing=8,
            controls=[
                ft.Row([
                    ft.Icon(icon, color=icon_color, size=20),
                    ft.Text(title, size=12, weight=ft.FontWeight.BOLD, color=ft.Colors.WHITE54),
                ]),
                value_control,
            ],
        ),
    )
