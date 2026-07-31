"""Contracts for the optional uninstall farewell screen.

These are static tests on purpose: CI does not install or uninstall Balachky,
and the Inno compiler is not required for the contract checks.
"""

import importlib.util
import re
import runpy
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ISS_PATH = ROOT / "installer" / "balachky.iss"
GENERATED_PATH = ROOT / "installer" / "farewell-data.iss"
GENERATOR_PATH = ROOT / "scripts" / "generate_installer_farewell_data.py"
LINKS_PATH = ROOT / "fronts" / "desktop" / "links.py"
CRASH_PATH = ROOT / "fronts" / "desktop" / "crash.py"

ISS = ISS_PATH.read_text(encoding="utf-8")

SOURCE_NAMES = (
    "ISSUE_URL",
    "SUPPORT_MONO_UAH",
    "SUPPORT_PRIVAT_USD",
    "SUPPORT_PRIVAT_EUR",
    "SUPPORT_USDT_TRC20",
    "SUPPORT_BTC",
    "SUPPORT_ETH",
)

DEFINE_NAMES = (
    "FarewellIssueUrl",
    "FarewellSupportMonoUah",
    "FarewellSupportPrivatUsd",
    "FarewellSupportPrivatEur",
    "FarewellSupportUsdtTrc20",
    "FarewellSupportBtc",
    "FarewellSupportEth",
)


def _section(name: str) -> str:
    match = re.search(rf"^\[{re.escape(name)}\]\s*$", ISS, re.MULTILINE)
    if not match:
        raise AssertionError(f"Missing [{name}] section")
    next_section = re.search(r"^\[.+\]\s*$", ISS[match.end() :], re.MULTILINE)
    if not next_section:
        return ISS[match.end() :]
    return ISS[match.end() : match.end() + next_section.start()]


def _initialize_uninstall_body() -> str:
    start = ISS.index("function InitializeUninstall(): Boolean;")
    end = ISS.index("\nend;", start) + len("\nend;")
    return ISS[start:end]


def _custom_messages(language: str) -> dict[str, str]:
    result = {}
    prefix = f"{language}."
    for line in _section("CustomMessages").splitlines():
        if line.startswith(prefix) and "=" in line:
            key, value = line.split("=", 1)
            result[key.removeprefix(prefix)] = value
    return result


def _button_metric(button: str, metric: str) -> str:
    match = re.search(
        rf"^\s*{re.escape(button)}\.{re.escape(metric)}\s*:=\s*(.+?);\s*$",
        _initialize_uninstall_body(),
        flags=re.MULTILINE,
    )
    if not match:
        raise AssertionError(f"Missing {button}.{metric}")
    return match.group(1)


def _load_generator():
    spec = importlib.util.spec_from_file_location(
        "generate_installer_farewell_data", GENERATOR_PATH
    )
    if spec is None or spec.loader is None:
        raise AssertionError("Could not load the farewell-data generator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InstallerFarewellTests(unittest.TestCase):
    def test_silent_guard_exits_before_any_farewell_ui(self):
        body = _initialize_uninstall_body()
        self.assertIn(
            "if UninstallSilent() then",
            body,
            "Silent uninstall must exit instead of creating farewell UI",
        )
        guard = body.index("if UninstallSilent() then")
        exit_statement = body.index("Exit;", guard)
        first_form = body.index("CreateCustomForm", exit_statement)
        self.assertLess(guard, exit_statement)
        self.assertLess(exit_statement, first_form)

    def test_just_uninstall_completes_without_another_step(self):
        body = _initialize_uninstall_body()
        self.assertIn("FarewellUninstallButton.ModalResult := mrOk;", body)
        self.assertIn("Form.ActiveControl := FarewellUninstallButton;", body)
        self.assertEqual(
            body.count("Form.ShowModal()"),
            1,
            "The main uninstall path must not add another modal step",
        )
        result_branch = body[
            body.index("if Form.ShowModal() = mrOk then") :
        ]
        self.assertIn("RemoveUserData := DataCheck.Checked", result_branch)
        self.assertNotIn("UninstallMsgBox", result_branch)
        self.assertNotIn("CreateCustomForm", result_branch)

    def test_three_choices_have_identical_geometry(self):
        buttons = (
            "FarewellFeedbackButton",
            "FarewellSupportButton",
            "FarewellUninstallButton",
        )
        for metric in ("Top", "Width", "Height"):
            values = {_button_metric(button, metric) for button in buttons}
            self.assertEqual(
                len(values),
                1,
                f"Farewell buttons differ in {metric}: {values}",
            )

    def test_ukrainian_and_english_farewell_copy_are_paired(self):
        uk = _custom_messages("ukrainian")
        en = _custom_messages("english")
        prefixes = ("UninstFarewell", "UninstSupport", "UninstFeedback")
        uk_keys = {key for key in uk if key.startswith(prefixes)}
        en_keys = {key for key in en if key.startswith(prefixes)}
        self.assertEqual(uk_keys, en_keys)
        required = {
            "UninstFarewellPrompt",
            "UninstFarewellFeedbackBtn",
            "UninstFarewellSupportBtn",
            "UninstSupportTitle",
            "UninstSupportPrompt",
            "UninstSupportBackBtn",
            "UninstFeedbackError",
        }
        self.assertTrue(required.issubset(uk_keys))
        self.assertEqual(
            uk["UninstFarewellFeedbackBtn"],
            "Щось не працювало / чогось бракувало",
        )
        self.assertEqual(uk["UninstFarewellSupportBtn"], "Було корисно")
        self.assertEqual(uk["UninstRemoveBtn"], "Просто видалити")
        self.assertEqual(en["UninstRemoveBtn"], "Just uninstall")

    def test_generated_details_match_the_apps_single_source(self):
        self.assertTrue(
            GENERATOR_PATH.exists(),
            "Missing generator that reads fronts/desktop/links.py",
        )
        self.assertTrue(
            GENERATED_PATH.exists(),
            "Missing generated Inno include with embedded farewell data",
        )
        source = runpy.run_path(str(LINKS_PATH))
        expected = dict(zip(DEFINE_NAMES, (source[name] for name in SOURCE_NAMES)))

        generated = GENERATED_PATH.read_text(encoding="utf-8")
        generator = _load_generator()
        self.assertEqual(generated, generator.render_include(source))
        actual = dict(
            re.findall(r'^#define\s+(\w+)\s+"([^"]*)"$', generated, re.MULTILINE)
        )
        self.assertEqual(actual, expected)
        self.assertIn('#include "farewell-data.iss"', ISS)

        for value in expected.values():
            with self.subTest(value=value):
                self.assertNotIn(
                    value,
                    ISS,
                    "Support/feedback data must not be hard-coded in balachky.iss",
                )

    def test_generator_output_follows_changed_source_values(self):
        self.assertTrue(GENERATOR_PATH.exists(), "Missing farewell-data generator")
        generator = _load_generator()
        synthetic = {
            name: f"sentinel-{index}"
            for index, name in enumerate(SOURCE_NAMES, start=1)
        }
        rendered = generator.render_include(synthetic)
        for value in synthetic.values():
            with self.subTest(value=value):
                self.assertIn(value, rendered)

    def test_crash_dialog_and_uninstaller_share_the_existing_issue_channel(self):
        crash = CRASH_PATH.read_text(encoding="utf-8")
        self.assertIn("from .links import ISSUE_URL", crash)
        self.assertNotIn("_ISSUE_URL =", crash)
        self.assertIn("ISSUE_URL +", crash)

    def test_pascal_script_has_balanced_static_structure(self):
        code = _section("Code")
        code = re.sub(r"\{.*?\}", "", code, flags=re.DOTALL)
        code = re.sub(r"'(?:''|[^'])*'", "''", code)
        tokens = re.findall(
            r"\b(begin|try|case|end)\b",
            code,
            flags=re.IGNORECASE,
        )
        depth = 0
        for token in tokens:
            if token.lower() == "end":
                depth -= 1
                self.assertGreaterEqual(depth, 0, "Unmatched Pascal 'end'")
            else:
                depth += 1
        self.assertEqual(depth, 0, "Unclosed Pascal block")
        self.assertEqual(code.count("("), code.count(")"))

    def test_pascal_script_uses_documented_dialog_api(self):
        code = _section("Code")
        self.assertNotIn(
            "UninstallMsgBox(",
            code,
            "Inno exposes MsgBox in uninstall code, not UninstallMsgBox",
        )
        self.assertIn("MsgBox(", code)

    def test_pascal_references_resolve_statically(self):
        code = _section("Code")
        messages = set(_custom_messages("ukrainian"))
        references = set(re.findall(r"\{cm:(Uninst\w+)\}", code))
        self.assertTrue(references.issubset(messages))

        handlers = set(re.findall(r"\.OnClick\s*:=\s*@(\w+);", code))
        declarations = set(
            re.findall(
                r"^procedure\s+(\w+)\(Sender:\s*TObject\);",
                code,
                flags=re.MULTILINE,
            )
        )
        self.assertTrue(handlers.issubset(declarations))


if __name__ == "__main__":
    unittest.main()
