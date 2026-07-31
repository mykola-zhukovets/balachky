"""Static contracts for safe install, upgrade, and uninstall testing."""

import re
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ISS_PATH = ROOT / "installer" / "balachky.iss"
SCRIPT_PATH = ROOT / "scripts" / "test_install_matrix.ps1"
SANDBOX_PATH = ROOT / "dev" / "sandbox-install-test.wsb"
QA_GATE_PATH = ROOT / "docs" / "QA-GATE.md"

APP_ID = "{2C5BBCE3-5047-47A6-96B0-C78B12E059F9}"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def section(source: str, name: str) -> str:
    match = re.search(
        rf"(?ims)^\[{re.escape(name)}\]\s*$"
        rf"(.*?)(?=^\[[^\]]+\]\s*$|\Z)",
        source,
    )
    return "" if match is None else match.group(1)


def directives(source: str, name: str) -> list[str]:
    return [
        line.strip()
        for line in section(source, name).splitlines()
        if line.strip() and not line.lstrip().startswith(";")
    ]


def marked_ids(source: str, begin: str, end: str) -> list[str]:
    match = re.search(
        rf"(?s){re.escape(begin)}(.*?){re.escape(end)}",
        source,
    )
    assert match is not None, f"немає маркерів {begin} … {end}"
    return re.findall(r'["`]([a-z][a-z0-9-]+)["`]', match.group(1))


def test_app_id_is_the_stable_release_identity():
    setup = section(read(ISS_PATH), "Setup")
    matches = re.findall(r"(?im)^AppId=(.+?)\s*$", setup)
    assert matches == ["{" + APP_ID]


def test_installer_is_strictly_per_user():
    source = read(ISS_PATH)
    setup = section(source, "Setup")
    assert re.search(r"(?im)^PrivilegesRequired=lowest\s*$", setup)
    assert not re.search(r"(?im)^PrivilegesRequiredOverridesAllowed=", setup)
    assert re.search(
        r"(?im)^DefaultDirName=\{localappdata\}\\Programs\\Balachky\s*$",
        setup,
    )
    assert not re.search(r"(?i)\bHKLM\b", section(source, "Registry"))
    assert "HKEY_LOCAL_MACHINE" not in section(source, "Code").upper()


def test_registry_directives_are_hkcu_and_have_uninstall_cleanup_flags():
    records = directives(read(ISS_PATH), "Registry")
    for record in records:
        assert re.search(r"(?i)\bRoot:\s*HKCU\b", record), record
        assert re.search(
            r"(?i)\bFlags:.*\buninsdelete(?:key|value)\b",
            record,
        ), record


def test_application_registry_cleanup_remains_explicit_delete_data_only():
    source = read(ISS_PATH)
    code = section(source, "Code")
    assert "/REMOVEUSERDATA" not in source.upper()
    assert (
        "if (CurUninstallStep = usPostUninstall) and RemoveUserData then"
        in code
    )
    assert (
        "RegDeleteKeyIncludingSubkeys("
        "HKEY_CURRENT_USER, 'Software\\Balachky');"
        in code
    )


def test_silent_uninstall_keeps_user_data_by_default():
    code = section(read(ISS_PATH), "Code")
    initialize = code.split("function InitializeUninstall(): Boolean;", 1)[1]
    initialize = initialize.split(
        "procedure CurUninstallStepChanged", 1
    )[0]
    default_false = initialize.index("RemoveUserData := False;")
    silent_check = initialize.index("if UninstallSilent() then")
    silent_exit = initialize.index("Exit;", silent_check)
    assert default_false < silent_check < silent_exit


def test_uninstall_delete_entries_cannot_broadly_delete_user_files():
    records = directives(read(ISS_PATH), "UninstallDelete")
    assert records == [
        'Type: filesandordirs; Name: "{localappdata}\\Balachky"; '
        "Check: RemoveUserDataChecked",
        'Type: filesandordirs; Name: "{%TEMP}\\balachky-meeting-*"',
        'Type: filesandordirs; Name: "{%TEMP}\\balachky-meeting-media-*"',
        'Type: filesandordirs; Name: "{%TEMP}\\balachky-tts-plain-*"',
    ]


def test_matrix_script_has_safety_gate_before_process_launch():
    source = read(SCRIPT_PATH)
    assert "IAmInSandbox" in source
    assert "WDAGUtilityAccount" in source
    gate = source.index("# SAFETY_GATE_PASSED")
    assert source.index("IAmInSandbox") < gate
    assert source.index("WDAGUtilityAccount") < gate
    assert source.index("Start-Process") > gate


def test_matrix_script_uses_documented_silent_contracts_and_state_checks():
    source = read(SCRIPT_PATH)
    for switch in ("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"):
        assert switch in source
    assert '"/LOG=`"$logPath`""' in source
    assert "ExitCode" in source
    assert "Get-FileHash" in source
    assert r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall" in source
    assert 'Join-Path $env:LOCALAPPDATA "Balachky"' in source
    assert 'Join-Path $env:LOCALAPPDATA "Programs\\Balachky"' in source
    assert (
        "Assert-True (-not "
        "[string]::IsNullOrWhiteSpace($ExpectedMarkerHash))"
        in source
    )
    assert "PASS" in source
    assert "FAIL" in source


def test_script_and_qa_gate_have_the_same_scenario_ids():
    script_ids = marked_ids(
        read(SCRIPT_PATH),
        "# SCENARIO_IDS_BEGIN",
        "# SCENARIO_IDS_END",
    )
    documented_ids = marked_ids(
        read(QA_GATE_PATH),
        "<!-- INSTALL_MATRIX_SCENARIOS_BEGIN -->",
        "<!-- INSTALL_MATRIX_SCENARIOS_END -->",
    )
    assert script_ids
    assert len(script_ids) == len(set(script_ids))
    assert script_ids == documented_ids


def test_windows_sandbox_is_offline_read_only_and_autostarts_matrix():
    tree = ET.parse(SANDBOX_PATH)
    root = tree.getroot()

    assert root.tag == "Configuration"
    assert root.findtext("Networking") == "Disable"

    mappings = root.findall("./MappedFolders/MappedFolder")
    assert len(mappings) == 2
    assert all(mapping.findtext("ReadOnly") == "true" for mapping in mappings)
    sandbox_folders = {
        mapping.findtext("SandboxFolder") for mapping in mappings
    }
    assert sandbox_folders == {
        r"C:\BalachkyInstallers",
        r"C:\BalachkyScripts",
    }

    command = root.findtext("./LogonCommand/Command") or ""
    assert r"C:\BalachkyScripts\test_install_matrix.ps1" in command
    assert "-IAmInSandbox" in command
    assert "-OldInstaller" in command
    assert "-NewInstaller" in command
