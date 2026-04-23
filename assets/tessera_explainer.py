"""Tessera explainer — 60-90 second animated walkthrough.

Three acts:
  1. Problem: AI tools each keep their own siloed memory; the user repeats
     themselves every time they switch tools.
  2. Solution: one local sqlcipher-encrypted vault on disk; every MCP
     client reads and writes through capability-scoped tokens.
  3. Demo: capture in Claude, recall cross-facet bundle in ChatGPT,
     draft in the user's voice.

No external assets. Text primitives only (no MathTex dependency).
Color palette: 5 intentional colors, each encoding meaning.
"""

from __future__ import annotations

from manim import (
    BLUE_D,
    DOWN,
    GREEN,
    GREY,
    LEFT,
    ORIGIN,
    PURPLE_B,
    RED_E,
    RIGHT,
    UP,
    WHITE,
    YELLOW,
    Arrow,
    Circle,
    Create,
    FadeIn,
    FadeOut,
    Indicate,
    LaggedStart,
    Rectangle,
    RoundedRectangle,
    Scene,
    Text,
    VGroup,
    Write,
)

# Intentional palette — each color encodes meaning.
C_TESSERA = BLUE_D  # the vault itself
C_CAPTURE = GREEN  # capture / write path
C_RECALL = PURPLE_B  # recall / read path
C_CLIENT = GREY  # MCP clients
C_SILO = RED_E  # the problem: siloed memory
C_PRINCIPLE = YELLOW  # differentiating principles


class TesseraExplainer(Scene):
    def construct(self) -> None:
        self._act_one_problem()
        self._fade_all()
        self._act_two_solution()
        self._fade_all()
        self._act_three_demo()
        self._fade_all()
        self._closing_card()

    # ----------------------------------------------------------------
    # Act 1 — the problem
    # ----------------------------------------------------------------
    def _act_one_problem(self) -> None:
        title = Text("AI tools each keep their own memory.", font_size=42, weight="BOLD")
        title.to_edge(UP, buff=0.6)
        self.play(Write(title), run_time=1.4)
        self.wait(0.8)

        # Four tool tiles arranged horizontally.
        tool_names = ("Claude", "ChatGPT", "Cursor", "Codex")
        tools = VGroup(*[self._tool_tile(name) for name in tool_names])
        tools.arrange(RIGHT, buff=0.5)
        tools.move_to(ORIGIN + UP * 0.3)
        self.play(
            LaggedStart(*[FadeIn(tile, shift=UP * 0.3) for tile in tools], lag_ratio=0.18),
            run_time=1.8,
        )
        self.wait(0.5)

        # Each tool gets its own little silo box underneath it.
        silos = VGroup(*[self._silo_block() for _ in tool_names])
        for silo, tool in zip(silos, tools, strict=True):
            silo.next_to(tool, DOWN, buff=0.3)
        self.play(
            LaggedStart(*[FadeIn(s) for s in silos], lag_ratio=0.15),
            run_time=1.2,
        )
        self.wait(0.5)

        pain_label = Text(
            "You repeat yourself. Every tool. Every session.",
            font_size=28,
            color=C_SILO,
        )
        pain_label.to_edge(DOWN, buff=0.8)
        self.play(Write(pain_label), run_time=1.4)
        self.wait(1.8)

        # Pulse the silos red to reinforce the fragmentation.
        self.play(
            LaggedStart(
                *[Indicate(s, color=C_SILO, scale_factor=1.15) for s in silos], lag_ratio=0.25
            ),
            run_time=2.0,
        )
        self.wait(2.2)

    def _tool_tile(self, name: str) -> VGroup:
        box = RoundedRectangle(
            width=2.2, height=1.2, corner_radius=0.15, color=C_CLIENT, stroke_width=3
        )
        label = Text(name, font_size=26).move_to(box.get_center())
        return VGroup(box, label)

    def _silo_block(self) -> VGroup:
        block = Rectangle(width=1.8, height=0.6, color=C_SILO, stroke_width=2)
        label = Text("memory", font_size=16, color=C_SILO).move_to(block.get_center())
        return VGroup(block, label)

    # ----------------------------------------------------------------
    # Act 2 — the solution
    # ----------------------------------------------------------------
    def _act_two_solution(self) -> None:
        title = Text("One vault. Every tool. On your disk.", font_size=42, weight="BOLD")
        title.to_edge(UP, buff=0.6)
        self.play(Write(title), run_time=1.4)
        self.wait(0.5)

        # The vault, center.
        vault = RoundedRectangle(
            width=3.0, height=1.8, corner_radius=0.2, color=C_TESSERA, stroke_width=4
        )
        vault_label = Text("Tessera vault", font_size=26, color=C_TESSERA, weight="BOLD")
        vault_label.move_to(vault.get_center() + UP * 0.3)
        vault_sub = Text("sqlcipher + SQLite", font_size=18, color=C_TESSERA)
        vault_sub.move_to(vault.get_center() + DOWN * 0.3)
        vault_group = VGroup(vault, vault_label, vault_sub)
        vault_group.move_to(ORIGIN)
        self.play(Create(vault), Write(vault_label), Write(vault_sub), run_time=1.4)
        self.wait(0.8)

        # Four clients around the vault, connected by arrows.
        tool_names = ("Claude", "ChatGPT", "Cursor", "Codex")
        positions = [
            LEFT * 5.0 + UP * 1.5,
            RIGHT * 5.0 + UP * 1.5,
            LEFT * 5.0 + DOWN * 1.5,
            RIGHT * 5.0 + DOWN * 1.5,
        ]
        clients = []
        arrows = []
        for name, pos in zip(tool_names, positions, strict=True):
            tile = self._small_tile(name)
            tile.move_to(pos)
            clients.append(tile)
            arrow = Arrow(
                start=tile.get_center(),
                end=vault.get_center(),
                buff=1.0,
                color=C_TESSERA,
                stroke_width=3,
            )
            arrows.append(arrow)
        self.play(
            LaggedStart(*[FadeIn(t) for t in clients], lag_ratio=0.15),
            run_time=1.4,
        )
        self.play(
            LaggedStart(*[Create(a) for a in arrows], lag_ratio=0.15),
            run_time=1.4,
        )
        self.wait(0.5)

        mcp_label = Text(
            "MCP + capability tokens",
            font_size=22,
            color=C_TESSERA,
        )
        mcp_label.next_to(vault, DOWN, buff=1.0)
        self.play(FadeIn(mcp_label), run_time=0.8)
        self.wait(1.0)

        # Five facet-type chips flowing into the vault.
        facet_types = ("identity", "preference", "workflow", "project", "style")
        chips = VGroup(*[self._facet_chip(ft) for ft in facet_types])
        chips.arrange(RIGHT, buff=0.25)
        chips.to_edge(DOWN, buff=0.4)
        self.play(
            LaggedStart(*[FadeIn(c, shift=UP * 0.2) for c in chips], lag_ratio=0.15),
            run_time=1.8,
        )
        self.wait(1.6)

        # Principles badges (brief).
        principles = Text(
            "all-local  •  zero telemetry  •  encrypted at rest",
            font_size=22,
            color=C_PRINCIPLE,
        )
        principles.next_to(mcp_label, DOWN, buff=0.3)
        self.play(Write(principles), run_time=1.6)
        self.wait(3.0)

    def _small_tile(self, name: str) -> VGroup:
        box = RoundedRectangle(
            width=1.8, height=0.9, corner_radius=0.12, color=C_CLIENT, stroke_width=3
        )
        label = Text(name, font_size=22).move_to(box.get_center())
        return VGroup(box, label)

    def _facet_chip(self, ft: str) -> VGroup:
        box = RoundedRectangle(
            width=1.9, height=0.55, corner_radius=0.15, color=C_TESSERA, stroke_width=2
        )
        label = Text(ft, font_size=18, color=WHITE).move_to(box.get_center())
        return VGroup(box, label)

    # ----------------------------------------------------------------
    # Act 3 — the demo
    # ----------------------------------------------------------------
    def _act_three_demo(self) -> None:
        title = Text("Capture once. Recall anywhere.", font_size=42, weight="BOLD")
        title.to_edge(UP, buff=0.6)
        self.play(Write(title), run_time=1.0)

        # Left half: Claude captures.
        left_header = Text("Claude Desktop", font_size=26, color=C_CLIENT)
        left_header.move_to(LEFT * 3.5 + UP * 2.0)
        capture_steps = VGroup(
            self._step_line("capture preference", C_CAPTURE),
            self._step_line("capture workflow", C_CAPTURE),
            self._step_line("capture project", C_CAPTURE),
            self._step_line("capture style", C_CAPTURE),
        )
        capture_steps.arrange(DOWN, aligned_edge=LEFT, buff=0.3)
        capture_steps.next_to(left_header, DOWN, buff=0.5)

        # Right half: ChatGPT recalls.
        right_header = Text("ChatGPT Dev Mode", font_size=26, color=C_CLIENT)
        right_header.move_to(RIGHT * 3.5 + UP * 2.0)
        recall_call = self._step_line("recall(facet_types=all)", C_RECALL)
        recall_call.next_to(right_header, DOWN, buff=0.5)

        # Central vault (small) as the shared state.
        vault = RoundedRectangle(
            width=2.0, height=1.0, corner_radius=0.15, color=C_TESSERA, stroke_width=3
        )
        vault_label = Text("vault", font_size=22, color=C_TESSERA).move_to(vault.get_center())
        vault_group = VGroup(vault, vault_label).move_to(ORIGIN + DOWN * 0.3)

        self.play(
            FadeIn(left_header),
            FadeIn(right_header),
            Create(vault),
            Write(vault_label),
            run_time=1.4,
        )
        self.wait(0.5)

        # Capture steps land in the vault one by one.
        self.play(
            LaggedStart(*[FadeIn(s, shift=RIGHT * 0.2) for s in capture_steps], lag_ratio=0.35),
            run_time=3.5,
        )
        self.play(Indicate(vault_group, color=C_CAPTURE, scale_factor=1.15), run_time=1.2)
        self.wait(0.8)

        # Recall call fires, arrow goes vault → ChatGPT.
        self.play(FadeIn(recall_call, shift=LEFT * 0.2), run_time=1.0)
        recall_arrow = Arrow(
            start=vault.get_right(),
            end=recall_call.get_left(),
            buff=0.3,
            color=C_RECALL,
            stroke_width=3,
        )
        self.play(Create(recall_arrow), run_time=1.0)
        self.play(Indicate(recall_call, color=C_RECALL, scale_factor=1.15), run_time=1.0)

        # Outcome line.
        outcome = Text(
            "cross-facet bundle → draft in the user's voice",
            font_size=24,
            color=C_PRINCIPLE,
        )
        outcome.to_edge(DOWN, buff=0.7)
        self.play(Write(outcome), run_time=1.6)
        self.wait(3.2)

    def _step_line(self, label: str, color: str) -> VGroup:
        dot = Circle(radius=0.1, color=color, fill_opacity=1.0, stroke_width=0)
        text = Text(label, font_size=22, color=WHITE)
        text.next_to(dot, RIGHT, buff=0.25)
        return VGroup(dot, text)

    # ----------------------------------------------------------------
    # Closing
    # ----------------------------------------------------------------
    def _closing_card(self) -> None:
        name = Text("Tessera", font_size=72, weight="BOLD", color=C_TESSERA)
        tag = Text(
            "Portable context for T-shaped AI-native users.",
            font_size=28,
            color=WHITE,
        )
        home = Text(
            "github.com/Mathews-Tom/Tessera",
            font_size=22,
            color=GREY,
        )
        VGroup(name, tag, home).arrange(DOWN, buff=0.4).move_to(ORIGIN)
        self.play(Write(name), run_time=1.2)
        self.play(FadeIn(tag, shift=UP * 0.2), run_time=1.0)
        self.play(FadeIn(home), run_time=0.6)
        self.wait(2.8)

    # ----------------------------------------------------------------
    # Shared helpers
    # ----------------------------------------------------------------
    def _fade_all(self) -> None:
        if not self.mobjects:
            return
        self.play(
            *[FadeOut(mob) for mob in list(self.mobjects)],
            run_time=0.6,
        )
        self.wait(0.2)
