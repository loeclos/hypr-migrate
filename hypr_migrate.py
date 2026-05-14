#!/usr/bin/env python3
"""
hypr-migrate — Migrate Hyprland hyprlang .conf (≤0.54) to Lua .lua (≥0.55).

Three-phase semantic pipeline: Decompose → Sort → Reconstruct.
Emits only documented hl.* APIs matching the Hyprland wiki exactly.
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

# ──────────────────────────────────────────────────────────────────────
# PHASE 1 — DATA MODEL (semantic IR)
# ──────────────────────────────────────────────────────────────────────

class Severity(Enum):
    OK = 0
    WARNING = 1
    NOTE = 2
    ERROR = 3


@dataclass
class Annotation:
    kind: str       # MIGRATED | PATTERN | MERGED | MIGRATION_WARNING | MIGRATION_NOTE
    message: str


@dataclass
class SourceLoc:
    line: int
    source: str = ""


# Module-level helpers to annotate directives (avoids dataclass inheritance issues)
def warn(d: Any, msg: str):
    d.annotations.append(Annotation("MIGRATION_WARNING", msg))

def note(d: Any, msg: str):
    d.annotations.append(Annotation("MIGRATION_NOTE", msg))

def migrated(d: Any, msg: str = ""):
    d.annotations.append(Annotation("MIGRATED", msg))

def pat(d: Any, msg: str):
    d.annotations.append(Annotation("PATTERN", msg))


# All directive dataclasses have loc + annotations first, then payload fields.
# Use a factory lambda so 'annotations' defaults to field(default_factory=list).
_ANNO = lambda: field(default_factory=list)


@dataclass
class Variable:
    loc: SourceLoc
    name: str
    value: str
    annotations: list[Annotation] = _ANNO()


@dataclass
class ConfigVal:
    loc: SourceLoc
    section: str
    key: str
    value: str
    raw_value: str = ""
    annotations: list[Annotation] = _ANNO()


@dataclass
class MonitorEntry:
    loc: SourceLoc
    output: str
    mode: str = ""
    refresh: int = 0
    position: str = ""
    scale: float = 1.0
    transform: int = 0
    annotations: list[Annotation] = _ANNO()


@dataclass
class BindEntry:
    loc: SourceLoc
    mods: str
    key: str
    dispatcher: str
    param: str = ""
    description: str = ""
    flags: set = field(default_factory=set)
    is_unbind: bool = False
    is_mouse: bool = False
    is_locked: bool = False
    is_longpress: bool = False
    is_repeat: bool = False
    is_transient: bool = False
    is_non_consuming: bool = False
    is_ignore_mods: bool = False
    annotations: list[Annotation] = _ANNO()


@dataclass
class WindowRule:
    loc: SourceLoc
    effect: str
    match_raw: str
    match_class: str = ""
    match_title: str = ""
    match_xwayland: Optional[bool] = None
    match_floating: Optional[bool] = None
    match_fullscreen: Optional[bool] = None
    match_pinned: Optional[bool] = None
    match_workspace: str = ""
    match_onworkspace: str = ""
    is_v2: bool = False
    annotations: list[Annotation] = _ANNO()


@dataclass
class WsRule:
    loc: SourceLoc
    workspace: str
    monitor: str = ""
    gaps_in: Optional[int] = None
    gaps_out: Optional[int] = None
    border: Optional[int] = None
    decorate: Optional[bool] = None
    persistent: Optional[bool] = None
    default: Optional[bool] = None
    annotations: list[Annotation] = _ANNO()


@dataclass
class EnvVar:
    loc: SourceLoc
    key: str
    value: str
    annotations: list[Annotation] = _ANNO()


@dataclass
class ExecCmd:
    loc: SourceLoc
    command: str
    once: bool = False
    annotations: list[Annotation] = _ANNO()


@dataclass
class SourceEntry:
    loc: SourceLoc
    path: str
    annotations: list[Annotation] = _ANNO()


@dataclass
class BezierEntry:
    loc: SourceLoc
    name: str
    x1: float
    y1: float
    x2: float
    y2: float
    annotations: list[Annotation] = _ANNO()


@dataclass
class LayerRule:
    loc: SourceLoc
    rule: str
    namespace: str
    annotations: list[Annotation] = _ANNO()


@dataclass
class WsBindEntry:
    loc: SourceLoc
    class_name: str
    workspace: str
    annotations: list[Annotation] = _ANNO()


@dataclass
class AnimationEntry:
    loc: SourceLoc
    name: str
    enabled: bool = True
    speed: float = 5.0
    curve: str = ""
    style: str = ""
    annotations: list[Annotation] = _ANNO()


@dataclass
class GestureEntry:
    loc: SourceLoc
    fingers: int
    direction: str
    action: str
    threshold: Optional[int] = None
    annotations: list[Annotation] = _ANNO()


@dataclass
class UnknownDirective:
    loc: SourceLoc
    raw: str
    annotations: list[Annotation] = _ANNO()


# ──────────────────────────────────────────────────────────────────────
# PHASE 1 — PARSER
# ──────────────────────────────────────────────────────────────────────

BOOL_MAP = {
    "yes": True, "no": False, "true": True, "false": False,
    "on": True, "off": False, "enabled": True, "disabled": False,
}

KNOWN_SECTIONS: set[str] = {
    "general", "decoration", "input", "gestures", "misc",
    "xwayland", "binds", "debug", "opengl", "cursor", "render",
    "group", "dwindle", "master", "animatelist", "windowing",
    "plugin", "environment",
}

SECTION_ALIASES: dict[str, str] = {
    "animatelist": "animation",
}

BIND_VARIANTS = {"bind", "bindl", "bindle", "bindm", "bindr", "binde",
                 "bindn", "bindi", "bindt", "unbind"}


def normalize_val(raw: str, line: int) -> tuple[str, bool, bool, bool]:
    """Return (lua_value_str, is_number, is_bool, is_str)."""
    raw = raw.strip()
    if not raw:
        return '""', False, False, True
    # color
    if raw.startswith("0x") or raw.lower().startswith("rgb("):
        return _norm_color(raw), False, False, True
    # boolean
    if raw.lower() in BOOL_MAP:
        return str(BOOL_MAP[raw.lower()]).lower(), False, True, False
    # number (int)
    if re.fullmatch(r"-?\d+", raw):
        return raw, True, False, False
    # number (float)
    if re.fullmatch(r"-?\d+(\.\d+)?", raw):
        return raw, True, False, False
    return f'"{raw}"', False, False, True


def _norm_color(raw: str) -> str:
    """Normalize hyprlang colors → rgba(rrggbbaa) string for Lua."""
    raw = raw.strip()
    if raw.startswith("0x"):
        hex_part = raw[2:]
        if len(hex_part) > 8:
            hex_part = hex_part[:8]
        # 0xAARRGGBB → rgba(RRGGBBAA)
        if len(hex_part) == 8:
            aa, rr, gg, bb = hex_part[0:2], hex_part[2:4], hex_part[4:6], hex_part[6:8]
            return f'"rgba({rr}{gg}{bb}{aa})"'
        # 0xRRGGBB → rgba(RRGGBBff)
        if len(hex_part) == 6:
            return f'"rgba({hex_part}ff)"'
        return f'"rgba({hex_part}ff)"'
    # rgb(rrggbb) or rgba(rrggbbaa) — already in css-style notation
    m = re.match(r"(?i)rgba?\s*\(\s*([a-f0-9]+)\s*\)", raw)
    if m:
        hex_part = m.group(1)
        if len(hex_part) == 6:
            return f'"rgba({hex_part}ff)"'
        return f'"rgba({hex_part})"'
    return f'"{raw}"'


def _parse_int(s: str, default: int = 0) -> int:
    try:
        return int(s.strip())
    except (ValueError, AttributeError):
        return default


def _parse_float(s: str, default: float = 0.0) -> float:
    try:
        return float(s.strip().rstrip("%"))
    except (ValueError, AttributeError):
        return default


def _is_section_header(line: str) -> tuple[bool, str]:
    m = re.match(r"^(\w[\w-]*)\s*\{\s*$", line)
    if m:
        return True, m.group(1)
    if re.match(r"^\s*\}\s*$", line):
        return True, "}"
    return False, ""


class HyprlangParser:
    def __init__(self, path: str):
        self.path = path
        self.ir = ConfigIR()
        self.ir.source_path = path
        self._variables: dict[str, str] = {}
        self._section_stack: list[str] = []
        self._lines: list[str] = []

    def parse(self) -> "ConfigIR":
        raw = self._read()
        self._lines = self._preprocess(raw)
        for i, line in enumerate(self._lines):
            self._parse_line(line.strip(), i + 1)
        self._resolve_variables()
        self._finalize()
        return self.ir

    def _read(self) -> list[str]:
        with open(self.path) as f:
            return f.readlines()

    def _preprocess(self, raw: list[str]) -> list[str]:
        out: list[str] = []
        acc: list[str] = []
        for line in raw:
            stripped = line.rstrip("\n")
            if stripped.endswith("\\") and not stripped.endswith("\\\\"):
                acc.append(stripped[:-1])
                continue
            if acc:
                acc.append(stripped)
                out.append("".join(acc))
                acc = []
            else:
                out.append(stripped)
        if acc:
            out.append("".join(acc))
        # strip inline comments (but not inside strings)
        cleaned: list[str] = []
        for line in out:
            idx = _find_comment_start(line)
            if idx is not None:
                cleaned.append(line[:idx])
            else:
                cleaned.append(line)
        return cleaned

    def _parse_line(self, line: str, lineno: int):
        if not line:
            return
        loc = SourceLoc(lineno, line)

        # Section close
        if line == "}":
            if self._section_stack:
                self._section_stack.pop()
            return

        # Section open
        is_sec, sec_name = _is_section_header(line)
        if is_sec and sec_name != "}":
            self._section_stack.append(sec_name)
            return

        curr_sec = ".".join(self._section_stack) if self._section_stack else ""

        # $variable
        if line.startswith("$"):
            m = re.match(r"\$(\w[\w-]*)\s*=\s*(.*?)\s*$", line)
            if m:
                var = Variable(loc=loc, name=m.group(1), value=m.group(2).strip())
                self.ir.variables.append(var)
                self._variables[var.name] = var.value
            return

        # source
        if line.lower().startswith("source"):
            path = _split_kv(line)[1] if "=" in line else _split_kv(line)[0]
            self.ir.sources.append(SourceEntry(loc=loc, path=path.strip()))
            return

        # env
        if line.lower().startswith("env"):
            _, rest = _split_kv(line)
            parts = _split_csv(rest, 2)
            key = parts[0].strip() if parts else ""
            val = parts[1].strip() if len(parts) > 1 else ""
            self.ir.envs.append(EnvVar(loc=loc, key=key, value=val))
            return

        # exec / exec-once
        if line.lower().startswith("exec-once"):
            _, cmd = _split_kv(line, raw_rhs=True)
            self.ir.execs.append(ExecCmd(loc=loc, command=cmd.strip(), once=True))
            return
        if line.lower().startswith("exec "):
            _, cmd = _split_kv(line, raw_rhs=True)
            self.ir.execs.append(ExecCmd(loc=loc, command=cmd.strip(), once=False))
            return

        # monitor
        if line.lower().startswith("monitor"):
            self._parse_monitor(line, loc)
            return

        # bind / unbind / bindl / bindle / bindm / binde
        lc = line.lower()
        bind_type = ""
        for variant in BIND_VARIANTS:
            if lc.startswith(variant) and (len(lc) == len(variant) or lc[len(variant)] in (" ", "=")):
                bind_type = variant
                break
        if bind_type:
            self._parse_bind(line, bind_type, loc)
            return

        # windowrule / windowrulev2
        if lc.startswith("windowrule") or lc.startswith("windowrulev2"):
            self._parse_windowrule(line, loc)
            return

        # workspace
        if line.lower().startswith("workspace"):
            self._parse_workspace(line, loc)
            return

        # bezier
        if line.lower().startswith("bezier"):
            self._parse_bezier(line, loc)
            return

        # layerrule
        if line.lower().startswith("layerrule"):
            _, rest = _split_kv(line)
            parts = _split_csv(rest, 2)
            rule = parts[0].strip() if parts else ""
            ns = parts[1].strip() if len(parts) > 1 else ""
            self.ir.layer_rules.append(LayerRule(loc=loc, rule=rule, namespace=ns))
            return

        # wsbind
        if line.lower().startswith("wsbind"):
            _, rest = _split_kv(line)
            parts = _split_csv(rest, 2)
            cls = parts[0].strip() if parts else ""
            ws = parts[1].strip() if len(parts) > 1 else ""
            self.ir.ws_binds.append(WsBindEntry(loc=loc, class_name=cls, workspace=ws))
            return

        # animation = NAME, ENABLED, SPEED, [CURVE], [STYLE]
        if line.lower().startswith("animation"):
            _, rest = _split_kv(line)
            parts = _split_csv(rest, 5)
            if len(parts) >= 3:
                name = parts[0].strip()
                enabled = parts[1].strip() in ("1", "yes", "true", "on")
                speed = _parse_float(parts[2], 5.0)
                curve = parts[3].strip() if len(parts) > 3 else ""
                style = parts[4].strip() if len(parts) > 4 else ""
                self.ir.animations.append(AnimationEntry(
                    loc=loc, name=name, enabled=enabled,
                    speed=speed, curve=curve, style=style,
                ))
            return

        # gesture inside gestures section
        if "=" in line and curr_sec and curr_sec.split(".")[-1] == "gestures":
            key, val = _split_kv(line)
            if key == "gesture":
                parts = _split_csv(val, 4)
                if len(parts) >= 3:
                    fingers = _parse_int(parts[0], 3)
                    direction = parts[1].strip()
                    action = parts[2].strip()
                    threshold = _parse_int(parts[3]) if len(parts) > 3 else None
                    self.ir.gestures.append(GestureEntry(
                        loc=loc, fingers=fingers, direction=direction,
                        action=action, threshold=threshold,
                    ))
                    return

        # generic key=value inside a section → ConfigVal
        if "=" in line and curr_sec:
            key, val = _split_kv(line)
            cv = ConfigVal(
                loc=loc, section=curr_sec,
                key=key.strip(), value=val.strip(), raw_value=val.strip(),
            )
            self.ir.config_vals.append(cv)
            return

        # top-level k=v (might be legacy decoration= graphemes etc)
        if "=" in line:
            key, val = _split_kv(line)
            if key == "gesture":
                parts = _split_csv(val, 4)
                if len(parts) >= 3:
                    self.ir.gestures.append(GestureEntry(
                        loc=loc, fingers=_parse_int(parts[0], 3),
                        direction=parts[1].strip(), action=parts[2].strip(),
                        threshold=_parse_int(parts[3]) if len(parts) > 3 else None,
                    ))
                    return
            cv = ConfigVal(
                loc=loc, section="",
                key=key.strip(), value=val.strip(), raw_value=val.strip(),
            )
            self.ir.config_vals.append(cv)
            return

        # unknown / unrecognized
        self.ir.unknown.append(UnknownDirective(loc=loc, raw=line))

    # -- sub-parsers ---------------------------------------------------------

    MONITOR_RE = re.compile(
        r"(?:monitor\s*[=,]\s*)?"
        r"(?P<output>\S+)\s*"
        r"(?:,\s*(?P<mode>preferred|highres|highrr|\d+x\d+)\s*"
        r"(?:@(?P<refresh>\d+))?\s*)?"
        r"(?:,\s*(?P<pos>(?:-?\d+x-?\d+|auto)))?"
        r"(?:,\s*(?P<scale>[\d.]+))?"
        r"(?:,\s*(?P<transform>\d))?"
        r".*$"
    )

    def _parse_monitor(self, line: str, loc: SourceLoc):
        line = line.strip()
        # Strip leading directive
        rest = re.sub(r"(?i)monitor\s*[=,]\s*", "", line, count=1).strip()
        parts = _split_csv(rest)
        if not parts:
            return
        m = MonitorEntry(loc=loc, output=parts[0])
        if len(parts) > 1:
            m.mode = parts[1]
            if "@" in parts[1]:
                mm = re.match(r"(\d+x\d+)@(\d+)", parts[1])
                if mm:
                    m.mode = mm.group(1)
                    m.refresh = int(mm.group(2))
        if len(parts) > 2:
            m.position = parts[2]
        if len(parts) > 3:
            m.scale = _parse_float(parts[3], 1.0)
        if len(parts) > 4:
            m.transform = _parse_int(parts[4], 0)
        self.ir.monitors.append(m)

    FLAG_MAP: dict[str, str] = {
        "l": "locked", "r": "repeat", "e": "longpress",
        "n": "non_consuming", "m": "mouse", "t": "transient",
        "i": "ignore_mods",
    }

    def _parse_bind(self, line: str, bind_type: str, loc: SourceLoc):
        is_unbind = bind_type == "unbind"
        is_mouse = bind_type == "bindm"
        is_locked = bind_type in ("bindl", "bindle")
        is_longpress = bind_type in ("binde", "bindle")

        # extract flags from the variant name (characters after "bind")
        flags: set[str] = set()
        m_flags = re.match(r"(?i)bind([lrenmti]*)\s*[=,]\s*", line)
        flag_str = ""
        if m_flags:
            flag_str = m_flags.group(1) if m_flags.group(1) else ""
        for ch in flag_str:
            flags.add(ch)
        is_repeat = "r" in flag_str.lower()

        _, rest = _split_kv(line, raw_rhs=True)
        parts = _split_csv(rest)
        if not parts:
            return

        is_transient = "t" in flags
        is_non_consuming = "n" in flags
        is_ignore_mods = "i" in flags

        e = BindEntry(
            loc=loc, flags=flags, is_unbind=is_unbind, is_mouse=is_mouse,
            is_locked=is_locked, is_longpress=is_longpress, is_repeat=is_repeat,
            is_transient=is_transient, is_non_consuming=is_non_consuming,
            is_ignore_mods=is_ignore_mods,
            mods=parts[0].strip() if len(parts) > 0 else "",
            key=parts[1].strip() if len(parts) > 1 else "",
            dispatcher=parts[2].strip() if len(parts) > 2 else "",
            param=parts[3].strip() if len(parts) > 3 else "",
            description=", ".join(p.strip() for p in parts[4:]) if len(parts) > 4 else "",
        )
        self.ir.binds.append(e)

    WINDOWRULE_RE = re.compile(
        r"(windowrule|windowrulev2)\s*[=,]\s*(.*)", re.IGNORECASE
    )

    def _parse_windowrule(self, line: str, loc: SourceLoc):
        m = self.WINDOWRULE_RE.match(line.strip())
        if not m:
            return
        is_v2 = m.group(1).lower() == "windowrulev2"
        rest = m.group(2)
        parts = _split_csv(rest)
        if not parts:
            return
        effect = parts[0].strip()
        match_raw = ", ".join(p.strip() for p in parts[1:])

        wr = WindowRule(loc=loc, effect=effect, match_raw=match_raw, is_v2=is_v2)
        self._parse_window_match(wr, match_raw)
        self.ir.window_rules.append(wr)

    MATCH_FIELD_RE = re.compile(
        r"(class|title|xwayland|floating|fullscreen|pinned|noinitialfocus|"
        r"noblur|noshadow|nofocus|workspace|initialworkspace|onworkspace|"
        r"tag|noanim|nodim|noalpha|noshadow|nomaxsize|border)"
        r"\s*:\s*(.+?)(?=,\s*\w+\s*:|\s*$)"
    )

    def _parse_window_match(self, wr: WindowRule, raw: str):
        wr.match_raw = raw
        fields = self._extract_match_fields(raw)
        for k, v in fields.items():
            if k == "class":
                wr.match_class = v
            elif k == "title":
                wr.match_title = v
            elif k == "xwayland":
                wr.match_xwayland = v in ("1", "true", "yes")
            elif k == "floating":
                wr.match_floating = v in ("1", "true", "yes")
            elif k == "fullscreen":
                wr.match_fullscreen = v in ("1", "true", "yes")
            elif k == "pinned":
                wr.match_pinned = v in ("1", "true", "yes")
            elif k == "workspace":
                wr.match_workspace = v
            elif k == "onworkspace":
                wr.match_onworkspace = v

    @staticmethod
    def _extract_match_fields(raw: str) -> dict[str, str]:
        fields: dict[str, str] = {}
        if not raw.strip():
            return fields
        # Split on comma, but be careful about commas inside regex/patterns
        tokens = _split_csv(raw)
        for token in tokens:
            token = token.strip()
            if ":" in token:
                k, _, v = token.partition(":")
                fields[k.strip().lower()] = v.strip()
            else:
                # bare string — treat as class name
                if "class" not in fields:
                    fields["class"] = token
        return fields

    def _parse_workspace(self, line: str, loc: SourceLoc):
        _, rest = _split_kv(line)
        parts = _split_csv(rest)
        if not parts:
            return
        ws = WsRule(loc=loc, workspace=parts[0].strip())
        for p in parts[1:]:
            p = p.strip()
            if ":" in p:
                k, _, v = p.partition(":")
                k = k.strip().lower()
                v = v.strip()
                if k == "monitor":
                    ws.monitor = v
                elif k == "gapsin" or k == "gaps_in":
                    ws.gaps_in = _parse_int(v)
                elif k == "gapsout" or k == "gaps_out":
                    ws.gaps_out = _parse_int(v)
                elif k == "border":
                    ws.border = _parse_int(v)
                elif k == "decorate":
                    ws.decorate = v.lower() in ("1", "yes", "true", "on")
                elif k == "persistent":
                    ws.persistent = v.lower() in ("1", "yes", "true", "on")
                elif k == "default":
                    ws.default = v.lower() in ("1", "yes", "true", "on")
        self.ir.workspace_rules.append(ws)

    def _parse_bezier(self, line: str, loc: SourceLoc):
        _, rest = _split_kv(line)
        parts = _split_csv(rest)
        if len(parts) < 5:
            return
        be = BezierEntry(
            loc=loc, name=parts[0].strip(),
            x1=_parse_float(parts[1]), y1=_parse_float(parts[2]),
            x2=_parse_float(parts[3]), y2=_parse_float(parts[4]),
        )
        self.ir.beziers.append(be)

    # -- variable resolution -------------------------------------------------

    def _resolve_variables(self):
        """Two-pass variable resolution."""
        for _ in range(2):
            for var in self.ir.variables:
                var.value = self._subst_vars(var.value)
        # Apply to all string values
        self._apply_vars_to_ir()

    def _subst_vars(self, s: str) -> str:
        def _repl(m: re.Match) -> str:
            key = m.group(1) or m.group(2)
            return self._variables.get(key, m.group(0))
        return re.sub(r"\$\{(\w[\w-]*)\}|\$(\w[\w-]*)", _repl, s)

    def _apply_vars_to_ir(self):
        for cv in self.ir.config_vals:
            cv.value = self._subst_vars(cv.value)
            cv.raw_value = cv.value
        for e in self.ir.execs:
            e.command = self._subst_vars(e.command)
        for e in self.ir.envs:
            e.value = self._subst_vars(e.value)
        for e in self.ir.sources:
            e.path = self._subst_vars(e.path)
        for m in self.ir.monitors:
            m.output = self._subst_vars(m.output)
            m.mode = self._subst_vars(m.mode)
            m.position = self._subst_vars(m.position)
        for b in self.ir.binds:
            b.mods = self._subst_vars(b.mods)
            b.param = self._subst_vars(b.param)
            b.description = self._subst_vars(b.description)
        for wr in self.ir.window_rules:
            wr.match_raw = self._subst_vars(wr.match_raw)
            wr.match_class = self._subst_vars(wr.match_class)
        for ws in self.ir.workspace_rules:
            ws.monitor = self._subst_vars(ws.monitor)

    def _finalize(self):
        """Collect stats."""
        ir = self.ir
        ir.num_variables = len(ir.variables)
        ir.num_config_keys = len(ir.config_vals)
        ir.num_monitors = len(ir.monitors)
        ir.num_binds = len(ir.binds)
        ir.num_window_rules = len(ir.window_rules)
        ir.num_workspace_rules = len(ir.workspace_rules)
        ir.num_execs = len(ir.execs)
        ir.num_sources = len(ir.sources)
        ir.num_gestures = len(ir.gestures)


# ──────────────────────────────────────────────────────────────────────
# CONFIG IR  (output of parser, input to sorter/emitter)
# ──────────────────────────────────────────────────────────────────────

@dataclass
class ConfigIR:
    source_path: str = ""
    variables: list[Variable] = field(default_factory=list)
    config_vals: list[ConfigVal] = field(default_factory=list)
    monitors: list[MonitorEntry] = field(default_factory=list)
    binds: list[BindEntry] = field(default_factory=list)
    window_rules: list[WindowRule] = field(default_factory=list)
    workspace_rules: list[WsRule] = field(default_factory=list)
    envs: list[EnvVar] = field(default_factory=list)
    execs: list[ExecCmd] = field(default_factory=list)
    sources: list[SourceEntry] = field(default_factory=list)
    beziers: list[BezierEntry] = field(default_factory=list)
    layer_rules: list[LayerRule] = field(default_factory=list)
    ws_binds: list[WsBindEntry] = field(default_factory=list)
    animations: list[AnimationEntry] = field(default_factory=list)
    gestures: list[GestureEntry] = field(default_factory=list)
    unknown: list[UnknownDirective] = field(default_factory=list)

    num_variables: int = 0
    num_config_keys: int = 0
    num_monitors: int = 0
    num_binds: int = 0
    num_binds_collapsed: int = 0
    num_window_rules: int = 0
    num_window_rules_merged: int = 0
    num_workspace_rules: int = 0
    num_execs: int = 0
    num_sources: int = 0
    num_gestures: int = 0
    num_warnings: int = 0
    num_notes: int = 0

    def all_directives(self) -> list[Directive]:
        """Return all directives for scanning annotations."""
        return (self.variables + self.config_vals + self.monitors + self.binds +
                self.window_rules + self.workspace_rules + self.envs + self.execs +
                self.sources + self.beziers + self.layer_rules + self.ws_binds +
                self.animations + self.gestures + self.unknown)

    def count_annotations(self, kind: str) -> int:
        return sum(
            1 for d in self.all_directives()
            for a in d.annotations if a.kind == kind
        )


# ──────────────────────────────────────────────────────────────────────
# PHASE 2 — SORTER & PATTERN DETECTOR
# ──────────────────────────────────────────────────────────────────────

class Sorter:
    """Sort directives into canonical order and detect patterns."""

    def __init__(self, ir: ConfigIR):
        self.ir = ir

    def sort(self) -> "Sorter":
        self._group_config_vals()
        self._detect_ws_bind_loop()
        self._detect_merged_window_rules()
        self._detect_conflicts()
        self._annotate_bind_exit()
        self._update_stats()
        return self

    def _group_config_vals(self):
        """ConfigVals are kept in order within each section, but sections are
        emitted in a canonical order."""
        pass  # grouping is done by the emitter via iteration

    def _detect_ws_bind_loop(self):
        """Detect sequential SUPER+N workspace binds and collapse them."""
        binds = self.ir.binds
        i = 0
        while i < len(binds):
            run = self._find_ws_bind_run(binds, i)
            if run and len(run) >= 3:
                first, last = run[0], run[-1]
                # Determine workspace numbers
                ws_nums = []
                for b in run:
                    ws_nums.append(_parse_int(b.param))
                # Check if sequential
                if self._is_sequential(ws_nums):
                    pat(first, f"Sequential workspace binds [{first.loc.line}-{last.loc.line}] collapsed to loop")
                    for b in run[1:]:
                        b.annotations.append(Annotation("PATTERN", "Collapsed into loop above"))
                    self.ir.num_binds_collapsed += len(run) - 1
                    i += len(run)
                    continue
            i += 1

    def _find_ws_bind_run(self, binds: list[BindEntry], start: int) -> list[BindEntry]:
        if start >= len(binds):
            return []
        b = binds[start]
        if b.dispatcher.lower() not in ("workspace", "movetoworkspace"):
            return []
        run = [b]
        expected_num = _parse_int(b.param)
        if expected_num < 1:
            return [b]
        for j in range(start + 1, len(binds)):
            nxt = binds[j]
            if (nxt.mods == b.mods and nxt.dispatcher.lower() == b.dispatcher.lower() and
                    _parse_int(nxt.param) == expected_num + (j - start)):
                run.append(nxt)
            else:
                break
        return run

    @staticmethod
    def _is_sequential(nums: list[int]) -> bool:
        if not nums:
            return False
        for i in range(1, len(nums)):
            if nums[i] != nums[i - 1] + 1:
                return False
        return True

    def _detect_merged_window_rules(self):
        """Merge window rules for the same class where possible."""
        rules = self.ir.window_rules
        seen: dict[str, list[WindowRule]] = {}
        merged_indices: set[int] = set()
        for idx, wr in enumerate(rules):
            key = (wr.match_class, wr.match_title)
            if key in seen:
                # Check if can merge (same match class/title, different effects)
                prev = seen[key][-1]
                if prev.match_xwayland == wr.match_xwayland and prev.match_floating == wr.match_floating:
                    seen[key].append(wr)
                    merged_indices.add(idx)
                    pat(prev, f"MERGED with line {wr.loc.line}")
                    wr.annotations.append(Annotation("MERGED", f"Merged into line {prev.loc.line}"))
                    self.ir.num_window_rules_merged += 1
                    continue
            seen.setdefault(key, []).append(wr)

    def _detect_conflicts(self):
        """Detect duplicate/conflicting entries."""
        # Config key conflicts: same section+key, different values
        seen_cfg: dict[tuple[str, str], ConfigVal] = {}
        for cv in self.ir.config_vals:
            key = (cv.section, cv.key)
            if key in seen_cfg:
                prev = seen_cfg[key]
                if prev.value != cv.value:
                    warn(prev, f"Conflicting value for {cv.section}.{cv.key}: "
                         f"'{prev.value}' (line {prev.loc.line}) vs '{cv.value}' (line {cv.loc.line})")
                    warn(cv, f"Conflicting value for {cv.section}.{cv.key}: "
                         f"'{cv.value}' (line {cv.loc.line}) vs '{prev.value}' (line {prev.loc.line})")
            else:
                seen_cfg[key] = cv

        # Windowrule conflicts: same match, different effect
        seen_wr: dict[str, WindowRule] = {}
        for wr in self.ir.window_rules:
            key = wr.match_raw or wr.match_class
            if key and key in seen_wr:
                prev = seen_wr[key]
                if prev.effect != wr.effect:
                    warn(prev, f"Window rule conflict for '{key}': "
                         f"'{prev.effect}' (line {prev.loc.line}) vs '{wr.effect}' (line {wr.loc.line})")
                    warn(wr, f"Window rule conflict for '{key}': "
                         f"'{wr.effect}' (line {wr.loc.line}) vs '{prev.effect}' (line {prev.loc.line})")
            elif key:
                seen_wr[key] = wr

        # Bind conflicts: same mods+key, different dispatcher/param
        seen_bind: dict[tuple[str, str], BindEntry] = {}
        for b in self.ir.binds:
            if b.is_unbind:
                continue
            key = (b.mods, b.key)
            if key in seen_bind:
                prev = seen_bind[key]
                if prev.dispatcher != b.dispatcher or prev.param != b.param:
                    warn(prev, f"Bind conflict for '{b.mods}, {b.key}': "
                         f"'{prev.dispatcher} {prev.param}' (line {prev.loc.line}) "
                         f"vs '{b.dispatcher} {b.param}' (line {b.loc.line})")
                    warn(b, f"Bind conflict for '{b.mods}, {b.key}': "
                         f"'{b.dispatcher} {b.param}' (line {b.loc.line}) "
                         f"vs '{prev.dispatcher} {prev.param}' (line {prev.loc.line})")
            else:
                seen_bind[key] = b

    def _annotate_bind_exit(self):
        """Annotate any bind using exit dispatcher."""
        for b in self.ir.binds:
            if b.dispatcher.lower() == "exit":
                note(b, "hl.dsp.exit() is not available in Lua; use uwsm stop instead (see wiki)")

    def _update_stats(self):
        ir = self.ir
        ir.num_warnings = ir.count_annotations("MIGRATION_WARNING")
        ir.num_notes = ir.count_annotations("MIGRATION_NOTE")


# ──────────────────────────────────────────────────────────────────────
# PHASE 3 — LUA EMITTER
# ──────────────────────────────────────────────────────────────────────

SECTION_ORDER = [
    "general", "decoration", "input", "misc",
    "binds", "xwayland", "debug", "opengl", "cursor", "render",
    "group", "dwindle", "master", "windowing",
]


class LuaEmitter:
    """Emits idiomatic Lua from the semantic IR."""

    def __init__(self, ir: ConfigIR):
        self.ir = ir
        self._lines: list[str] = []
        self._indent = 0

    def emit(self) -> str:
        self._lines = []
        # Annotate first (source→require, layerrule, wsbind, animation notes)
        self._annotate_remaining()
        self._count_final_stats()
        self._header()
        self._emit_variables()
        self._emit_envs()
        self._emit_sources()
        self._emit_config()
        self._emit_gestures()
        self._emit_monitors()
        self._emit_workspace_rules()
        self._emit_window_rules()
        self._emit_binds()
        self._emit_execs()
        self._emit_beziers()
        self._emit_remaining()
        self._footer()
        return "\n".join(self._lines)

    def _count_final_stats(self):
        ir = self.ir
        ir.num_warnings = ir.count_annotations("MIGRATION_WARNING")
        ir.num_notes = ir.count_annotations("MIGRATION_NOTE")

    def _annotate_remaining(self):
        """Pre-annotate directives that don't have direct Lua equivalents."""
        for s in self.ir.sources:
            note(s, "'source' maps to require(); verify the path resolves correctly in your module setup")
        for wb in self.ir.ws_binds:
            note(wb, "wsbind has no direct Lua equivalent; use hl.workspace_rule() with match instead")
        for m in self.ir.monitors:
            if not m.output:
                note(m, "Empty monitor output — Hyprland may ignore this entry")
        for u in self.ir.unknown:
            note(u, "Unrecognized directive — skipped")

    def _w(self, line: str = ""):
        if line:
            self._lines.append("  " * self._indent + line)
        else:
            self._lines.append("")

    def _header(self):
        src = os.path.basename(self.ir.source_path) if self.ir.source_path else "<input>"
        ir = self.ir
        w = ir.num_warnings
        n = ir.num_notes
        self._w(f"-- hyprland.lua")
        self._w(f"-- Generated by hypr-migrate from: {src}")
        self._w(f"-- Hyprland >= 0.55 required — https://wiki.hypr.land/")
        self._w(f"--")
        self._w(f"-- MIGRATION SUMMARY:")
        self._w(f"--   Variables:       {ir.num_variables} resolved")
        self._w(f"--   Config keys:     {ir.num_config_keys} emitted")
        self._w(f"--   Monitors:        {ir.num_monitors}")
        self._w(f"--   Keybinds:        {ir.num_binds} total, {ir.num_binds_collapsed} collapsed into loops")
        self._w(f"--   Window rules:    {ir.num_window_rules} total, {ir.num_window_rules_merged} merged")
        self._w(f"--   Workspace rules: {ir.num_workspace_rules}")
        self._w(f"--   Exec commands:   {ir.num_execs}")
        self._w(f"--   Gestures:        {ir.num_gestures}")
        self._w(f"--   Warnings:        {w}  ← search MIGRATION_WARNING")
        self._w(f"--   Review needed:   {n}  ← search MIGRATION_NOTE")
        self._w(f"--")
        self._w(f"-- IMPORTANT: Verify output against https://wiki.hypr.land/ before use")
        self._w(f"-- The wiki is the only authoritative reference for Lua syntax.")
        self._w(f"")

    def _footer(self):
        pass

    def _emit_variables(self):
        for var in self.ir.variables:
            for a in var.annotations:
                if a.kind in ("MIGRATED",):
                    continue
                self._w(f"-- {a.kind}: {a.message}")
            val_str, is_num, is_bool, is_str = normalize_val(var.value, var.loc.line)
            self._w(f"local {var.name} = {val_str}")
        if self.ir.variables:
            self._w()

    def _emit_envs(self):
        for e in self.ir.envs:
            for a in e.annotations:
                self._w(f"-- {a.kind}: {a.message}")
            self._w(f'hl.env("{e.key}", "{e.value}")')
        if self.ir.envs:
            self._w()

    def _emit_sources(self):
        for s in self.ir.sources:
            for a in s.annotations:
                self._w(f"-- {a.kind}: {a.message}")
            # Try to convert path to a Lua require format
            path = s.path
            if path.endswith(".conf"):
                path = path[:-5]
            if path.endswith("/"):
                path = path[:-1]
            path = path.replace("/", ".")
            self._w(f'-- MIGRATED: require("{path}")  -- was: source = {s.path}')
            self._w(f'require("{path}")')
        if self.ir.sources:
            self._w()

    def _emit_config(self):
        """Emit hl.config() with all sections merged — handles nested dotted paths."""
        if not self.ir.config_vals:
            return

        # Build a nested dict from dotted section paths
        def _ensure(config: dict, path: list[str]) -> dict:
            for p in path:
                config = config.setdefault(p, {})
            return config

        sections: dict[str, dict] = {}
        for cv in self.ir.config_vals:
            parts = [s for s in cv.section.split(".") if s] if cv.section else []
            d = _ensure(sections, parts)
            val_str, _, _, _ = normalize_val(cv.value, cv.loc.line)
            d[cv.key] = (val_str, [a for a in cv.annotations])

        # Sort top-level keys by canonical order
        def _sort_key(item: tuple) -> int:
            k, _ = item
            try:
                return SECTION_ORDER.index(k)
            except ValueError:
                return 999

        if not sections:
            return

        self._w("hl.config({")
        self._emit_nested_table(sections, 1)
        self._w("})")
        self._w()

    def _emit_gestures(self):
        if not self.ir.gestures:
            return
        for g in self.ir.gestures:
            for a in g.annotations:
                self._w(f"-- {a.kind}: {a.message}")
            fields = [
                f"fingers = {g.fingers}",
                f'direction = "{g.direction}"',
                f'action = "{g.action}"',
            ]
            if g.threshold is not None:
                fields.append(f"threshold = {g.threshold}")
            body = ", ".join(fields)
            self._w(f"hl.gesture({{ {body} }})")
        self._w()

    def _emit_nested_table(self, tbl: dict, indent: int):
        """Recursively emit a Lua table from a nested dict."""
        # Sort keys: canonical sections first, then alphabetically
        keys = sorted(tbl.keys(), key=lambda k: (
            SECTION_ORDER.index(k) if k in SECTION_ORDER else 999, k
        ))
        for k in keys:
            v = tbl[k]
            pad = "    " * indent
            if isinstance(v, dict):
                self._w(f"{pad}{k} = {{")
                self._emit_nested_table(v, indent + 1)
                self._w(f"{pad}}},")
            else:
                val_str, annotations = v
                for a in annotations:
                    self._w(f"{pad}-- {a.kind}: {a.message}")
                self._w(f"{pad}{k} = {val_str},")

    def _emit_monitors(self):
        for m in self.ir.monitors:
            for a in m.annotations:
                self._w(f"-- {a.kind}: {a.message}")
            d = {}
            d["output"] = f'"{m.output}"'
            if m.mode and m.mode not in ("preferred", "highres", "highrr", ""):
                d["mode"] = f'"{m.mode}"'
            if m.refresh:
                d["refresh"] = str(m.refresh)
            if m.position:
                d["position"] = f'"{m.position}"'
            if m.scale != 1.0:
                d["scale"] = f"{m.scale}"
            if m.transform:
                d["transform"] = str(m.transform)
            fields = ", ".join(f"{k} = {v}" for k, v in d.items())
            self._w(f"hl.monitor({{ {fields} }})")
        if self.ir.monitors:
            self._w()

    def _emit_workspace_rules(self):
        for ws in self.ir.workspace_rules:
            for a in ws.annotations:
                self._w(f"-- {a.kind}: {a.message}")
            fields = [f'workspace = "{ws.workspace}"']
            if ws.monitor:
                fields.append(f'monitor = "{ws.monitor}"')
            if ws.gaps_in is not None:
                fields.append(f"gaps_in = {ws.gaps_in}")
            if ws.gaps_out is not None:
                fields.append(f"gaps_out = {ws.gaps_out}")
            if ws.border is not None:
                fields.append(f"border_size = {ws.border}")
            if ws.decorate is not None:
                fields.append(f"decorate = {str(ws.decorate).lower()}")
            if ws.persistent is not None:
                fields.append(f"persistent = {str(ws.persistent).lower()}")
            body = ", ".join(fields)
            self._w(f"hl.workspace_rule({{ {body} }})")
        if self.ir.workspace_rules:
            self._w()

    def _emit_window_rules(self):
        """Emit hl.window_rule() for each rule, merging where possible."""
        # Group rules by their merged identity
        merged_groups: dict[str, list[WindowRule]] = {}
        seen_merged: set[int] = set()
        for wr in self.ir.window_rules:
            key = (wr.match_class, wr.match_title)
            if id(wr) in seen_merged:
                continue
            merged_groups.setdefault(str(key), []).append(wr)

        # Emit
        for group in merged_groups.values():
            primary = group[0]
            for a in primary.annotations:
                self._w(f"-- {a.kind}: {a.message}")

            # Build match table
            match_fields: list[str] = []
            if primary.match_class:
                match_fields.append(f'class = "{primary.match_class}"')
            if primary.match_title:
                match_fields.append(f'title = "{primary.match_title}"')
            if primary.match_xwayland is not None:
                match_fields.append(f"xwayland = {str(primary.match_xwayland).lower()}")
            if primary.match_floating is not None:
                match_fields.append(f"floating = {str(primary.match_floating).lower()}")
            if primary.match_fullscreen is not None:
                match_fields.append(f"fullscreen = {str(primary.match_fullscreen).lower()}")

            match_str = ", ".join(match_fields) if match_fields else primary.match_raw

            # Parse all effects into (key, value) pairs
            all_pairs: list[tuple[str, str]] = []
            for wr in group:
                all_pairs.extend(_parse_window_rule_effect(wr.effect))

            eff_fields = ", ".join(f"{k} = {v}" for k, v in all_pairs)
            if len(group) == 1:
                if match_fields:
                    self._w(f"hl.window_rule({{ match = {{ {match_str} }}, {eff_fields} }})")
                else:
                    self._w(f'hl.window_rule({{ match = "{primary.match_raw}", {eff_fields} }})')
            else:
                if len(group) <= 3 and len(all_pairs) <= 4:
                    self._w(f"hl.window_rule({{ match = {{ {match_str} }}, {eff_fields} }})")
                else:
                    self._w(f"hl.window_rule({{")
                    self._w(f"    match = {{ {match_str} }},")
                    for k, v in all_pairs:
                        self._w(f"    {k} = {v},")
                    self._w(f"}})")
        if self.ir.window_rules:
            self._w()

    def _emit_binds(self):
        """Emit hl.bind() calls, collapsing workspace loops."""
        binds = self.ir.binds
        i = 0
        while i < len(binds):
            b = binds[i]
            # Check if part of a collapsed loop
            has_collapse = any(a.kind == "PATTERN" and "Collapsed" in a.message for a in b.annotations)
            if has_collapse:
                i += 1
                continue

            # Check if this is the start of a loop
            run = self._find_ws_bind_run(binds, i)
            if run and len(run) >= 3 and self._is_sequential([_parse_int(x.param) for x in run]):
                self._emit_bind_loop(run)
                i += len(run)
                continue

            self._emit_single_bind(b)
            i += 1
        if self.ir.binds:
            self._w()

    def _find_ws_bind_run(self, binds: list[BindEntry], start: int) -> list[BindEntry]:
        """Same as Sorter._find_ws_bind_run."""
        if start >= len(binds):
            return []
        b = binds[start]
        if b.dispatcher.lower() not in ("workspace", "movetoworkspace"):
            return []
        run = [b]
        expected_num = _parse_int(b.param)
        if expected_num < 1:
            return [b]
        for j in range(start + 1, len(binds)):
            nxt = binds[j]
            if (nxt.mods == b.mods and nxt.dispatcher.lower() == b.dispatcher.lower() and
                    _parse_int(nxt.param) == expected_num + (j - start)):
                run.append(nxt)
            else:
                break
        return run

    @staticmethod
    def _is_sequential(nums: list[int]) -> bool:
        if not nums:
            return False
        for i in range(1, len(nums)):
            if nums[i] != nums[i - 1] + 1:
                return False
        return True

    def _emit_bind_loop(self, run: list[BindEntry]):
        first = run[0]

        start_num = _parse_int(first.param)
        end_num = _parse_int(len(run) > 0 and run[-1].param or first.param)
        mods = first.mods
        disp = first.dispatcher.lower()

        # Print annotations from the first bind in the collapsed run
        for a in first.annotations:
            if a.kind != "PATTERN":
                self._w(f"-- {a.kind}: {a.message}")

        self._w(f"-- PATTERN: {len(run)} sequential binds collapsed to loop")
        self._w(f"for i = {start_num}, {end_num} do")
        disp_fn = _dispatcher_to_lua(disp)
        if disp.lower() in ("workspace", "movetoworkspace"):
            self._w(f"    hl.bind(\"{mods} + \" .. (i % 10), hl.dsp.{disp_fn}({{ workspace = tostring(i % 10) }}))")
        else:
            self._w(f"    hl.bind(\"{mods} + \" .. (i % 10), hl.dsp.{disp_fn}({{ i }}))")
        self._w(f"end")
        self._w()

    @staticmethod
    def _bind_opts(b: BindEntry) -> str:
        parts = []
        if b.is_locked:
            parts.append("locked = true")
        if b.is_repeat:
            parts.append("repeating = true")
        if b.is_longpress:
            parts.append("longpress = true")
        if b.is_transient:
            parts.append("transient = true")
        if b.is_non_consuming:
            parts.append("non_consuming = true")
        if b.is_ignore_mods:
            parts.append("ignore_modifiers = true")
        if b.is_mouse:
            parts.append("mouse = true")
        if not parts:
            return ""
        return ", { " + ", ".join(parts) + " }"

    def _emit_single_bind(self, b: BindEntry):
        for a in b.annotations:
            self._w(f"-- {a.kind}: {a.message}")

        # Build mods string (use plus format per wiki)
        mod_str = f'"{b.mods} + {b.key}"'
        opts = self._bind_opts(b)

        if b.is_unbind:
            self._w(f"hl.unbind({mod_str}){opts}")
            return

        # Map dispatcher name to Lua function
        disp_fn = _dispatcher_to_lua(b.dispatcher)
        if b.dispatcher.lower() == "exec":
            # exec → function() hl.exec_cmd() end
            self._w(f"hl.bind({mod_str}, function()")
            self._w(f"    hl.exec_cmd(\"{b.param}\")")
            self._w(f"end){opts}")
        elif b.dispatcher.lower() == "exit":
            self._w(f"-- MIGRATION_NOTE: hl.dsp.exit() is not available; use uwsm stop instead")
            self._w(f"-- was: bind = {b.mods}, {b.key}, {b.dispatcher}, {b.param}")
        else:
            if b.param:
                param_str = _fmt_dsp_param(b.param)
                if b.dispatcher.lower() in ("workspace", "movetoworkspace"):
                    param_str = f'workspace = "{b.param}"'
                self._w(f"hl.bind({mod_str}, hl.dsp.{disp_fn}({{ {param_str} }})){opts}")
            else:
                self._w(f"hl.bind({mod_str}, hl.dsp.{disp_fn}()){opts}")

    def _emit_execs(self):
        if not self.ir.execs:
            return
        once_cmds = [e for e in self.ir.execs if e.once]
        always_cmds = [e for e in self.ir.execs if not e.once]

        if once_cmds:
            self._w(f"hl.on(\"hyprland.start\", function()")
            for e in once_cmds:
                for a in e.annotations:
                    self._w(f"    -- {a.kind}: {a.message}")
                self._w(f"    hl.exec_cmd(\"{e.command}\")")
            self._w(f"end)")
            self._w()
        if always_cmds:
            self._w(f"-- MIGRATION_NOTE: 'exec' (non-once) commands — these run on every reload.")
            self._w(f"-- Consider if they should be in hyprland.start or another event.")
            self._w(f"hl.on(\"hyprland.reload\", function()")
            for e in always_cmds:
                for a in e.annotations:
                    self._w(f"    -- {a.kind}: {a.message}")
                self._w(f"    hl.exec_cmd(\"{e.command}\")")
            self._w(f"end)")
            self._w()

    def _emit_beziers(self):
        for b in self.ir.beziers:
            for a in b.annotations:
                self._w(f"-- {a.kind}: {a.message}")
            self._w(f"hl.curve(\"{b.name}\", {{")
            self._w(f"    type = \"bezier\",")
            self._w(f"    points = {{ {{{b.x1}, {b.y1}}}, {{{b.x2}, {b.y2}}} }}")
            self._w(f"}})")
        if self.ir.beziers:
            self._w()

    def _emit_remaining(self):
        """Emit directives that don't have a 1:1 Lua equivalent as comments."""
        for lr in self.ir.layer_rules:
            for a in lr.annotations:
                self._w(f"-- {a.kind}: {a.message}")
            rule = lr.rule
            _off = rule.endswith(" off")
            if _off:
                rule = rule[:-4]
            bool_val = not _off
            self._w(f'hl.layer_rule({{ match = {{ namespace = "{lr.namespace}" }}, {rule} = {str(bool_val).lower()} }})')

        for wb in self.ir.ws_binds:
            for a in wb.annotations:
                self._w(f"-- {a.kind}: {a.message}")
            self._w(f"-- MIGRATED: wsbind = {wb.class_name}, {wb.workspace}")

        for an in self.ir.animations:
            for a in an.annotations:
                self._w(f"-- {a.kind}: {a.message}")
            self._w(f"hl.animation({{")
            self._w(f'    leaf = "{an.name}",')
            self._w(f"    enabled = {str(an.enabled).lower()},")
            self._w(f"    speed = {an.speed},")
            if an.curve:
                self._w(f'    curve = "{an.curve}",')
            if an.style:
                self._w(f'    style = "{an.style}",')
            self._w(f"}})")
        if self.ir.animations or self.ir.layer_rules:
            self._w()

        for u in self.ir.unknown:
            for a in u.annotations:
                self._w(f"-- {a.kind}: {a.message}")
            self._w(f"-- MIGRATION_NOTE: {u.raw}")


# ──────────────────────────────────────────────────────────────────────
# UTILITIES
# ──────────────────────────────────────────────────────────────────────

def _find_comment_start(line: str) -> Optional[int]:
    """Find # comment: only at line start or after whitespace, respecting strings."""
    in_str = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_str = not in_str
        elif ch == "#" and not in_str and (i == 0 or line[i - 1] in (" ", "\t")):
            return i
    return None


def _split_kv(line: str, raw_rhs: bool = False) -> tuple[str, str]:
    """Split 'key = value' into (key, value)."""
    eq = line.find("=")
    if eq < 0:
        return line.strip(), ""
    key = line[:eq].strip()
    val = line[eq + 1:].strip() if not raw_rhs else line[eq + 1:]
    return key, val


def _split_csv(line: str, max_parts: int = 0) -> list[str]:
    """Split comma-separated values, handling quoted strings and parens.
    If max_parts > 0, stop splitting after N-1 commas and include remainder
    as the last part."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    in_str = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_str = not in_str
            current.append(ch)
        elif ch in ("(", "[", "{"):
            depth += 1
            current.append(ch)
        elif ch in (")", "]", "}"):
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0 and not in_str:
            parts.append("".join(current).strip())
            current = []
            if max_parts and len(parts) >= max_parts - 1:
                # Everything after this comma is the final part
                rest = line[i+1:].strip()
                if rest:
                    parts.append(rest)
                return parts
        else:
            current.append(ch)
    if current:
        rest = "".join(current).strip()
        if rest:
            parts.append(rest)
    return parts


_DISP_MAP: dict[str, str] = {
    "workspace": "focus",
    "movetoworkspace": "focus",
}

def _parse_window_rule_effect(effect_str: str) -> list[tuple[str, str]]:
    """Parse a window rule effect into (key, value) pairs for Lua table.

    Boolean effects (float, tile, center, etc.) become key = true.
    Valued effects get their value normalized via normalize_val.
    Multiple pairs are returned for compound effects like 'workspace 2 silent'.
    """
    raw = effect_str.strip()
    if not raw:
        return []

    parts = raw.split()
    name = parts[0]

    # Known boolean-type effects
    bool_effects = {
        "float", "tile", "center", "noblur", "noshadow", "noanim",
        "pin", "group", "stayfocused", "maximize", "noborder",
        "noRounding", "norounding",
    }
    if name in bool_effects:
        key = "no_rounding" if name in ("noRounding", "norounding") else name
        return [(key, "true")]

    if name == "unset":
        return [("unset", "true")]

    # Valued effect — need at least one value token
    if len(parts) < 2:
        return [(name, "true")]

    # Handle workspace specially (can have "silent" modifier)
    if name == "workspace":
        ws_val = parts[1]
        pairs = [("workspace", f'"{ws_val}"')]
        if len(parts) > 2 and parts[2] == "silent":
            pairs.append(("silent", "true"))
        return pairs

    # Strip all "override" tokens from the value
    val_parts = [p for p in parts[1:] if p != "override"]
    val = " ".join(val_parts)

    # Let normalize_val handle quoting/color/bool detection
    norm_val, _, _, _ = normalize_val(val, 0)
    return [(name, norm_val)]


def _dispatcher_to_lua(disp: str) -> str:
    """Map hyprland dispatcher name to Lua function name."""
    return _DISP_MAP.get(disp.lower(), disp)


def _fmt_dsp_param(param: str) -> str:
    """Format a dispatcher parameter for Lua table insertion."""
    # If it's a number, don't quote
    if re.fullmatch(r"-?\d+(\.\d+)?", param):
        return param
    # If it's a simple word, don't quote
    if re.fullmatch(r"\w+", param):
        return param
    escaped = param.replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'


def _parse_int(s: str, default: int = 0) -> int:
    try:
        return int(s.strip())
    except (ValueError, AttributeError):
        return default


def _parse_float(s: str, default: float = 0.0) -> float:
    try:
        return float(s.strip().rstrip("%"))
    except (ValueError, AttributeError):
        return default


# ──────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────

def build_cli() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hypr-migrate",
        description="Migrate Hyprland hyprlang .conf (≤0.54) to Lua .lua (≥0.55)",
    )
    p.add_argument("input", metavar="<input.conf>", help="Hyprland .conf file to migrate")
    p.add_argument("--out", metavar="<file>", help="Write output to file (default: stdout)")
    p.add_argument("--in-place", action="store_true", help="Overwrite input as .lua, backup as .conf.bak")
    p.add_argument("--diff", action="store_true", help="Print unified diff of changes")
    p.add_argument("--dry-run", action="store_true", help="Parse and report warnings/notes without writing output")
    p.add_argument("--strict", action="store_true", help="Exit non-zero if any MIGRATION_NOTE is emitted")
    p.add_argument("-v", "--verbose", action="store_true", help="Print progress to stderr")
    return p


def main():
    parser = build_cli()
    args = parser.parse_args()

    if not os.path.isfile(args.input):
        print(f"error: file not found: {args.input}", file=sys.stderr)
        sys.exit(3)

    # Phase 1: Parse
    if args.verbose:
        print(f"hypr-migrate: parsing {args.input} ...", file=sys.stderr)
    try:
        p = HyprlangParser(args.input)
        ir = p.parse()
    except Exception as e:
        print(f"parse error: {e}", file=sys.stderr)
        sys.exit(3)

    if args.verbose:
        print(f"hypr-migrate: parsed {ir.num_variables} variables, "
              f"{ir.num_config_keys} config keys, {ir.num_monitors} monitors, "
              f"{ir.num_binds} binds, {ir.num_window_rules} window rules, "
              f"{ir.num_workspace_rules} ws rules, {ir.num_execs} execs",
              file=sys.stderr)

    # Phase 2: Sort & detect patterns
    if args.verbose:
        print("hypr-migrate: sorting and detecting patterns ...", file=sys.stderr)
    sorter = Sorter(ir)
    sorter.sort()

    if args.verbose:
        print(f"hypr-migrate: collapsed {ir.num_binds_collapsed} binds into loops, "
              f"merged {ir.num_window_rules_merged} window rules",
              file=sys.stderr)

    # Phase 3: Emit
    if args.verbose:
        print("hypr-migrate: emitting Lua ...", file=sys.stderr)
    emitter = LuaEmitter(ir)
    lua_output = emitter.emit()

    # Report warnings/notes
    if ir.num_warnings > 0:
        print(f"⚠  {ir.num_warnings} MIGRATION_WARNING(s) — check output for details", file=sys.stderr)
    if ir.num_notes > 0:
        print(f"✎  {ir.num_notes} MIGRATION_NOTE(s) — manual review required", file=sys.stderr)

    if args.dry_run:
        if ir.num_warnings:
            sys.exit(1)
        if ir.num_notes:
            sys.exit(2)
        sys.exit(0)

    # Determine output
    if args.in_place:
        base = os.path.splitext(args.input)[0]
        backup = args.input + ".bak"
        outpath = base + ".lua"
        if args.verbose:
            print(f"hypr-migrate: backing up to {backup}", file=sys.stderr)
        os.rename(args.input, backup)
        if args.verbose:
            print(f"hypr-migrate: writing {outpath}", file=sys.stderr)
        with open(outpath, "w") as f:
            f.write(lua_output)
        print(f"written: {outpath}  (backup: {backup})")
    elif args.out:
        with open(args.out, "w") as f:
            f.write(lua_output)
        print(f"written: {args.out}")
    elif args.diff:
        # Try to show diff against existing output
        diff_path = args.out or (os.path.splitext(args.input)[0] + ".lua")
        if os.path.isfile(diff_path):
            with open(diff_path) as f:
                old = f.read()
        else:
            old = ""
        diff = difflib.unified_diff(
            old.splitlines(keepends=True),
            lua_output.splitlines(keepends=True),
            fromfile=diff_path,
            tofile="<migrated>",
        )
        sys.stdout.writelines(diff)
    else:
        sys.stdout.write(lua_output)

    # Exit code
    if ir.num_warnings:
        if args.strict and ir.num_notes:
            sys.exit(2)
        sys.exit(1)
    if ir.num_notes:
        if args.strict:
            sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
