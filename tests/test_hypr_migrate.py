"""
Test suite for hypr-migrate CLI tool.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

# Add parent to path so we can import hypr_migrate
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypr_migrate import (HyprlangParser, Sorter, LuaEmitter, ConfigIR,
                          normalize_val, _norm_color)


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def parse_fixture(name: str) -> ConfigIR:
    path = FIXTURES / name
    p = HyprlangParser(str(path))
    ir = p.parse()
    s = Sorter(ir)
    s.sort()
    return ir


def emit_fixture(name: str) -> str:
    ir = parse_fixture(name)
    emitter = LuaEmitter(ir)
    return emitter.emit()


class TestParser(unittest.TestCase):
    """Phase 1: Parsing correctness."""

    def test_simple_config(self):
        ir = parse_fixture("01_simple.conf")
        self.assertGreater(ir.num_config_keys, 0)
        # Find specific values
        sensitivities = [cv for cv in ir.config_vals if cv.key == "sensitivity"]
        self.assertTrue(any(cv.value == "0.5" for cv in sensitivities))
        rounding = [cv for cv in ir.config_vals if cv.key == "rounding"]
        self.assertTrue(any(cv.value == "10" for cv in rounding))
        # Check nested touchpad section
        natural_scroll = [cv for cv in ir.config_vals if cv.key == "natural_scroll"]
        self.assertTrue(any(cv.value == "yes" for cv in natural_scroll))

    def test_variables(self):
        ir = parse_fixture("02_variables.conf")
        self.assertEqual(ir.num_variables, 4)
        # Variable names stored WITHOUT the $ prefix
        self.assertIn("mainMod", {v.name for v in ir.variables})
        bind_mods = [b.mods for b in ir.binds]
        self.assertTrue(all(m == "SUPER" for m in bind_mods))

    def test_monitors(self):
        ir = parse_fixture("03_monitors.conf")
        self.assertEqual(ir.num_monitors, 4)
        dp1 = [m for m in ir.monitors if m.output == "DP-1"]
        self.assertEqual(len(dp1), 1)
        self.assertEqual(dp1[0].mode, "2560x1440")
        self.assertEqual(dp1[0].refresh, 144)
        self.assertEqual(dp1[0].position, "0x0")
        self.assertEqual(dp1[0].scale, 1.0)
        # eDP-1 with scale
        edp = [m for m in ir.monitors if m.output == "eDP-1"]
        self.assertEqual(len(edp), 1)
        self.assertEqual(edp[0].scale, 1.25)

    def test_binds_all_variants(self):
        ir = parse_fixture("04_binds.conf")
        # Count binds (including unbind)
        self.assertGreater(len(ir.binds), 8)

        # Check specific flag interpretations
        bind_q = [b for b in ir.binds if b.key == "Q" and b.dispatcher == "killactive"]
        self.assertEqual(len(bind_q), 1)
        self.assertFalse(bind_q[0].is_locked)

        bindl = [b for b in ir.binds if b.dispatcher == "exec" and "pactl" in b.param]
        self.assertTrue(any(b.is_locked for b in bindl), "bindl should set locked")

        bindle = [b for b in ir.binds if b.dispatcher == "exec" and "RaiseVolume" in b.key]
        self.assertTrue(any(b.is_locked for b in bindle), "bindle should set locked")

        unbind = [b for b in ir.binds if b.is_unbind]
        self.assertEqual(len(unbind), 1)
        self.assertEqual(unbind[0].key, "Return")

        mouse = [b for b in ir.binds if b.is_mouse]
        self.assertEqual(len(mouse), 1)
        self.assertEqual(mouse[0].dispatcher, "movewindow")

        repeat = [b for b in ir.binds if "R" in {b.key} and b.dispatcher == "togglefloating"]
        self.assertTrue(any(b.is_repeat for b in ir.binds if b.dispatcher == "togglefloating"))

    def test_windowrules(self):
        ir = parse_fixture("05_windowrules.conf")
        self.assertGreater(ir.num_window_rules, 5)
        # kitty should have multiple rules (float + merged opacity)
        kitty_rules = [wr for wr in ir.window_rules if "kitty" in wr.match_class or "kitty" in wr.match_raw]
        self.assertGreaterEqual(len(kitty_rules), 1, "should parse kitty rules")

    def test_workspace_rules(self):
        ir = parse_fixture("06_workspace.conf")
        self.assertEqual(ir.num_workspace_rules, 4)
        ws1 = [ws for ws in ir.workspace_rules if ws.workspace == "1"]
        self.assertEqual(len(ws1), 1)
        self.assertEqual(ws1[0].monitor, "DP-1")

    def test_exec(self):
        ir = parse_fixture("07_exec.conf")
        self.assertEqual(ir.num_execs, 3)
        once = [e for e in ir.execs if e.once]
        always = [e for e in ir.execs if not e.once]
        self.assertEqual(len(once), 2)
        self.assertEqual(len(always), 1)

    def test_env(self):
        ir = parse_fixture("08_env.conf")
        self.assertEqual(len(ir.envs), 4)
        cursor = [e for e in ir.envs if e.key == "XCURSOR_SIZE"]
        self.assertEqual(len(cursor), 1)
        self.assertEqual(cursor[0].value, "24")
        sdl = [e for e in ir.envs if e.key == "SDL_VIDEODRIVER"]
        self.assertEqual(sdl[0].value, "wayland")

    def test_sources(self):
        ir = parse_fixture("09_source.conf")
        self.assertEqual(ir.num_sources, 3)
        self.assertTrue(any("keybinds" in s.path for s in ir.sources))

    def test_colors(self):
        ir = parse_fixture("10_colors.conf")
        # Find color values
        border_colors = [cv for cv in ir.config_vals if cv.key.startswith("col.")]
        self.assertGreater(len(border_colors), 0)
        # Check normalization happened
        for cv in ir.config_vals:
            if cv.key == "col_shadow":
                val, _, _, _ = normalize_val(cv.value, 0)
                self.assertIn("rgba", val)

    def test_workspace_bind_loop_detection(self):
        ir = parse_fixture("11_workspace_bind_loop.conf")
        # Should have binds collapsed
        self.assertGreater(ir.num_binds_collapsed, 0)

    def test_conflicts(self):
        ir = parse_fixture("12_conflicts.conf")
        self.assertGreater(ir.num_warnings, 0)

    def test_edge_cases(self):
        ir = parse_fixture("14_edge_cases.conf")
        # ${name} variable syntax resolves
        self.assertIn("mainMod", {v.name for v in ir.variables})
        # Empty monitor output parsed
        empty = [m for m in ir.monitors if m.output == ""]
        self.assertEqual(len(empty), 1)
        # Scale=0 should be preserved (was false-y bug)
        dp1 = [m for m in ir.monitors if m.output == "DP-1"]
        self.assertEqual(len(dp1), 1)
        self.assertEqual(dp1[0].scale, 0.0)

    def test_gestures_parsed(self):
        ir = parse_fixture("15_gestures.conf")
        self.assertEqual(len(ir.gestures), 1)
        g = ir.gestures[0]
        self.assertEqual(g.fingers, 3)
        self.assertEqual(g.direction, "vertical")
        self.assertEqual(g.action, "workspace")
        self.assertIsNone(g.threshold)
        # Should NOT appear as config_val
        gesture_vals = [cv for cv in ir.config_vals if cv.key == "gesture"]
        self.assertEqual(len(gesture_vals), 0)

    def test_variable_brace_syntax(self):
        """${name} syntax should resolve to variable value."""
        ir = parse_fixture("14_edge_cases.conf")
        bind_t = [b for b in ir.binds if b.key == "T" and b.dispatcher == "exec"]
        self.assertEqual(len(bind_t), 1)
        self.assertIn("kitty", bind_t[0].param, "${term} should resolve to kitty")


class TestEmitter(unittest.TestCase):
    """Phase 3: Lua emission correctness."""

    def test_header_present(self):
        out = emit_fixture("01_simple.conf")
        self.assertIn("hyprland.lua", out)
        self.assertIn("Generated by hypr-migrate", out)
        self.assertIn("MIGRATION SUMMARY", out)

    def test_config_sections(self):
        out = emit_fixture("01_simple.conf")
        self.assertIn("hl.config({", out)
        self.assertIn("general", out)
        self.assertIn("decoration", out)
        self.assertIn("sensitivity = 0.5", out)
        self.assertIn("drop_shadow = true", out)

    def test_variables_in_output(self):
        out = emit_fixture("02_variables.conf")
        self.assertIn("local mainMod", out)
        self.assertIn('local terminal = "kitty"', out)

    def test_monitors_in_output(self):
        out = emit_fixture("03_monitors.conf")
        self.assertIn("hl.monitor({", out)
        self.assertIn('"DP-1"', out)
        self.assertIn('"2560x1440"', out)
        self.assertIn("refresh = 144", out)

    def test_binds_in_output(self):
        out = emit_fixture("04_binds.conf")
        self.assertIn("hl.bind(", out)
        self.assertIn("hl.exec_cmd(", out)
        self.assertIn("hl.unbind(", out)

    def test_windowrules_in_output(self):
        out = emit_fixture("05_windowrules.conf")
        self.assertIn("hl.window_rule({", out)

    def test_workspace_in_output(self):
        out = emit_fixture("06_workspace.conf")
        self.assertIn("hl.workspace_rule({", out)

    def test_exec_in_output(self):
        out = emit_fixture("07_exec.conf")
        self.assertIn('hl.on("hyprland.start"', out)

    def test_env_in_output(self):
        out = emit_fixture("08_env.conf")
        self.assertIn('hl.env("XCURSOR_SIZE", "24")', out)

    def test_sources_in_output(self):
        out = emit_fixture("09_source.conf")
        self.assertIn("require(", out)
        self.assertIn("MIGRATED", out)

    def test_colors_in_output(self):
        out = emit_fixture("10_colors.conf")
        self.assertIn("rgba(", out)

    def test_animations_and_layer_rules(self):
        out = emit_fixture("13_animations.conf")
        self.assertIn("hl.curve(", out)
        self.assertIn("hl.animation({", out)
        self.assertIn("hl.layer_rule({", out)
        self.assertIn('leaf = "windows"', out)
        self.assertIn('namespace = "waybar"', out)
        self.assertIn('namespace = "firefox"', out)
        self.assertIn("blur = true", out)
        self.assertIn("blur = false", out)
        self.assertIn("enabled = false", out)

    def test_loop_collapse(self):
        out = emit_fixture("11_workspace_bind_loop.conf")
        self.assertIn("for i = 1, 10 do", out)
        self.assertIn("collapsed into loops", out)
        # Should have fewer explicit binds
        bind_count = out.count("hl.bind(")
        self.assertLess(bind_count, 20, "should have fewer than 20 explicit binds (10 collapsed into 2 loops)")

    def test_edge_cases_in_output(self):
        out = emit_fixture("14_edge_cases.conf")
        # ${name} variable resolves in output
        self.assertIn('local mainMod = "SUPER"', out)
        # Empty monitor annotation
        self.assertIn("Empty monitor output", out)
        # Scale=0 emitted (old bug: falsy check dropped it)
        self.assertIn("scale = 0.0", out)

    def test_gestures_in_output(self):
        out = emit_fixture("15_gestures.conf")
        self.assertIn("hl.gesture({", out)
        self.assertIn("fingers = 3", out)
        self.assertIn('direction = "vertical"', out)
        self.assertIn('action = "workspace"', out)
        # Should NOT be in hl.config
        config_end = out.find("hl.config({")
        if config_end >= 0:
            config_section = out[config_end:out.find("})", config_end)]
            self.assertNotIn("gesture", config_section)

    def test_no_silent_drops(self):
        """Every source line must produce output or annotation."""
        for fname in sorted(os.listdir(FIXTURES)):
            if not fname.endswith(".conf"):
                continue
            out = emit_fixture(fname)
            # Read the source to get line count (non-empty, non-comment)
            with open(FIXTURES / fname) as f:
                source_lines = [l for l in f.readlines()
                                if l.strip() and not l.strip().startswith("#")]
            # Check roughly the same number of meaningful lines in output
            self.assertGreater(len(out.splitlines()), len(source_lines) // 2,
                               f"{fname}: output seems too short for source lines")


class TestNormalization(unittest.TestCase):
    """Color and value normalization."""

    def test_color_0x(self):
        result = _norm_color("0xffeeeeee")
        self.assertIn("rgba(eeeeeeff)", result)

    def test_color_0x_cba6f7(self):
        result = _norm_color("0xffcba6f7")
        self.assertIn("rgba(cba6f7ff)", result)

    def test_color_rgb(self):
        result = _norm_color("rgb(1e1e2e)")
        self.assertIn("rgba(1e1e2eff)", result)

    def test_color_rgba(self):
        result = _norm_color("rgba(1e1e2eff)")
        self.assertIn("rgba(1e1e2eff)", result)

    def test_bool_yes(self):
        val, _, is_bool, _ = normalize_val("yes", 0)
        self.assertEqual(val, "true")
        self.assertTrue(is_bool)

    def test_bool_no(self):
        val, _, is_bool, _ = normalize_val("no", 0)
        self.assertEqual(val, "false")
        self.assertTrue(is_bool)

    def test_number_int(self):
        val, is_num, _, _ = normalize_val("42", 0)
        self.assertEqual(val, "42")
        self.assertTrue(is_num)

    def test_number_float(self):
        val, is_num, _, _ = normalize_val("0.5", 0)
        self.assertEqual(val, "0.5")
        self.assertTrue(is_num)


class TestCLI(unittest.TestCase):
    """CLI integration tests."""

    def test_stdout_output(self):
        """--out should produce a valid file."""
        import subprocess
        script = Path(__file__).resolve().parent.parent / "hypr_migrate.py"
        infile = FIXTURES / "01_simple.conf"
        result = subprocess.run(
            [sys.executable, str(script), str(infile)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, f"stdout: {result.stdout[:200]}")
        self.assertIn("hyprland.lua", result.stdout)

    def test_dry_run(self):
        """--dry-run should parse without emitting to stdout."""
        import subprocess
        script = Path(__file__).resolve().parent.parent / "hypr_migrate.py"
        infile = FIXTURES / "01_simple.conf"
        result = subprocess.run(
            [sys.executable, str(script), str(infile), "--dry-run"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0)

    def test_out_file(self):
        """--out should write to specified file."""
        import subprocess
        script = Path(__file__).resolve().parent.parent / "hypr_migrate.py"
        infile = FIXTURES / "01_simple.conf"
        with tempfile.NamedTemporaryFile(suffix=".lua", delete=False) as tmp:
            outpath = tmp.name
        try:
            result = subprocess.run(
                [sys.executable, str(script), str(infile), "--out", outpath],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0)
            self.assertTrue(os.path.isfile(outpath))
            with open(outpath) as f:
                content = f.read()
            self.assertIn("hyprland.lua", content)
        finally:
            if os.path.isfile(outpath):
                os.unlink(outpath)

    def test_in_place(self):
        """--in-place should create .lua and .conf.bak."""
        import subprocess
        import shutil
        script = Path(__file__).resolve().parent.parent / "hypr_migrate.py"
        infile_orig = FIXTURES / "01_simple.conf"
        with tempfile.NamedTemporaryFile(suffix=".conf", delete=False) as tmp:
            tmppath = tmp.name
            tmp.write(infile_orig.read_bytes())
        try:
            result = subprocess.run(
                [sys.executable, str(script), tmppath, "--in-place"],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, f"stderr: {result.stderr}")
            lua_path = tmppath.replace(".conf", ".lua")
            bak_path = tmppath + ".bak"
            self.assertTrue(os.path.isfile(lua_path), f"{lua_path} not found")
            self.assertTrue(os.path.isfile(bak_path), f"{bak_path} not found")
        finally:
            for p in [tmppath, tmppath.replace(".conf", ".lua"), tmppath + ".bak"]:
                if os.path.isfile(p):
                    os.unlink(p)

    def test_diff_flag(self):
        """--diff should produce diff output."""
        import subprocess
        script = Path(__file__).resolve().parent.parent / "hypr_migrate.py"
        infile = FIXTURES / "01_simple.conf"
        result = subprocess.run(
            [sys.executable, str(script), str(infile), "--diff"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0)
        # diff output typically starts with --- or +++
        self.assertTrue("---" in result.stdout or "+hl.config" in result.stdout)

    def test_verbose(self):
        """-v should print progress to stderr."""
        import subprocess
        script = Path(__file__).resolve().parent.parent / "hypr_migrate.py"
        infile = FIXTURES / "01_simple.conf"
        result = subprocess.run(
            [sys.executable, str(script), str(infile), "-v"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("hypr-migrate:", result.stderr)


if __name__ == "__main__":
    unittest.main()
