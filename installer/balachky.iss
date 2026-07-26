; Інсталер «Балачки у Коростені» — per-user, БЕЗ UAC (PrivilegesRequired=lowest).
; Збірка:  ISCC.exe installer\balachky.iss   (спершу pyinstaller balachky.spec)
; Результат: installer\Output\BalachkySetup-<версія>.exe

#include "version.iss"

#define AppExeName "Balachky.exe"
#define AppDisplayName "Балачки у Коростені"

[Setup]
; AppId — НЕ ЗМІНЮВАТИ НІКОЛИ: за цим GUID Windows та Inno впізнають
; встановлену програму (оновлення поверх, коректне видалення).
AppId={{2C5BBCE3-5047-47A6-96B0-C78B12E059F9}
AppName={#AppDisplayName}
AppVersion={#AppVersion}
AppVerName={#AppDisplayName} {#AppVersion}
AppPublisher=Mykola Zhukovets
VersionInfoVersion={#AppVersion}.0
VersionInfoCompany=Mykola Zhukovets
VersionInfoDescription={#AppDisplayName} Setup
VersionInfoProductName={#AppDisplayName}
VersionInfoProductVersion={#AppVersion}
; Ліцензія перед встановленням. Показуємо КОПІЮ з BOM (installer\LICENSE.txt):
; Inno без BOM читає файл як ANSI і кирилиця перетворюється на кашу. Копію
; стереже tests/test_installer_license.py — розійтися з LICENSE вона не може.
LicenseFile=LICENSE.txt
; Мінімальна ОС: без цього Inno дозволив би ставити й на Windows 7.
MinVersion=10.0
AppSupportURL=https://github.com/mykola-zhukovets/balachky/issues
AppUpdatesURL=https://github.com/mykola-zhukovets/balachky/releases
; AppMutex свідомо НЕ задано: єдиний екземпляр програма тримає через
; QLocalServer ("balachky-single", app.py), іменованого мутекса вона не
; створює — порожнє посилання було б мертвим налаштуванням. Запущену
; програму інсталятор бачить через CloseApplications (Restart Manager).
; per-user: без прав адміністратора, без UAC-вікна
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\Balachky
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=BalachkySetup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
SetupIconFile=..\assets\balachky.ico
UninstallDisplayIcon={app}\{#AppExeName}
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; закрити запущену програму перед оновленням (трей-застосунок)
CloseApplications=yes

[Languages]
Name: "ukrainian"; MessagesFile: "compiler:Languages\Ukrainian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

; Тексти кастомного вікна деінсталятора — двомовні (обирається за ActiveLanguage).
[CustomMessages]
ukrainian.UninstTitle=Балачки у Коростені — видалення
english.UninstTitle=Uninstall Balachky u Korosteni
ukrainian.UninstPrompt=Програму буде видалено. За замовчуванням Ваші налаштування, словники та історія зберігаються —  при повторному встановленні будуть доступні.
english.UninstPrompt=Balachky will be removed. Your settings, dictionaries, and history are kept by default, so they’ll be available if you reinstall it.
ukrainian.UninstRemoveData=Видалити локальні дані (Ваші налаштування, словники та історію)
english.UninstRemoveData=Also delete settings, dictionaries, and history
ukrainian.UninstRemoveBtn=Видалити
english.UninstRemoveBtn=Uninstall
ukrainian.UninstCancelBtn=Скасувати
english.UninstCancelBtn=Cancel

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

; Знести залишки ПОПЕРЕДНЬОЇ версії перед копіюванням нової. Inno сам видаляє
; лише те, що є в наборі: файл, який зник зі збірки, лежав би на диску вічно.
; Для нас це не косметика — у збірці 1.2.3 з озвученням поруч жив
; balachky-tts-worker.exe із torch/CUDA на 4,2 ГБ. Після оновлення на
; полегшену збірку він (а) з'їдав би 4 ГБ диска намарно, (б) що гірше —
; sidecar.engine_available() бачив би СТАРИЙ exe і вважав рушій наявним,
; хоч решта програми вже інша. Тому чистимо _internal і воркер явно.
[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
Type: files; Name: "{app}\balachky-tts-worker.exe"

[Files]
Source: "..\dist\Balachky\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppDisplayName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppDisplayName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#AppDisplayName}}"; Flags: nowait postinstall skipifsilent

; Користувацькі дані (%LOCALAPPDATA%\Balachky: config.toml, profiles\) інсталер
; типово НЕ чіпає — вони переживають перевстановлення і видалення програми
; свідомо. Видалення відбувається ЛИШЕ якщо користувач у деінсталяторі свідомо
; позначив «Видалити також налаштування і локальні дані» (див. [Code]).
[UninstallDelete]
Type: filesandordirs; Name: "{localappdata}\Balachky"; Check: RemoveUserDataChecked

[Code]
{ Прапорець вибору користувача в деінсталяторі; типово ВИМКНЕНО. }
var
  RemoveUserData: Boolean;

function RemoveUserDataChecked(): Boolean;
begin
  Result := RemoveUserData;
end;

{ Перед видаленням показуємо просте вікно з ЧЕКБОКСОМ. Дефолт — знято, тобто
  налаштування й локальні дані зберігаються (безпечна поведінка за замовч.).
  Лише коли користувач сам позначить — чистимо дані та реєстровий ключ.
  ТИХИЙ режим (/SILENT, /VERYSILENT — автооновлення чи IT-скрипт): вікно НЕ
  показуємо (інакше зависання без інтерактиву). Безпечний дефолт — дані НЕ
  чіпаємо (RemoveUserData := False), деінсталяцію продовжуємо. }
function InitializeUninstall(): Boolean;
var
  Form: TSetupForm;
  Lbl: TNewStaticText;
  DataCheck: TNewCheckBox;
  OkButton, CancelButton: TNewButton;
begin
  RemoveUserData := False;
  Result := True;

  if UninstallSilent() then
    Exit;   { тихий режим: без форми, дані зберігаємо, продовжуємо видалення }

  Form := CreateCustomForm(ScaleX(440), ScaleY(170), False, True);
  try
    Form.Caption := ExpandConstant('{cm:UninstTitle}');
    Form.Position := poScreenCenter;

    Lbl := TNewStaticText.Create(Form);
    Lbl.Parent := Form;
    Lbl.Left := ScaleX(16);
    Lbl.Top := ScaleY(16);
    Lbl.Width := ScaleX(408);
    Lbl.AutoSize := False;
    Lbl.Height := ScaleY(50);
    Lbl.WordWrap := True;
    Lbl.Caption := ExpandConstant('{cm:UninstPrompt}');

    DataCheck := TNewCheckBox.Create(Form);
    DataCheck.Parent := Form;
    DataCheck.Left := ScaleX(16);
    DataCheck.Top := ScaleY(78);
    DataCheck.Width := ScaleX(408);
    DataCheck.Height := ScaleY(40);
    DataCheck.Caption := ExpandConstant('{cm:UninstRemoveData}');
    DataCheck.Checked := False;

    OkButton := TNewButton.Create(Form);
    OkButton.Parent := Form;
    OkButton.Left := ScaleX(248);
    OkButton.Top := ScaleY(130);
    OkButton.Width := ScaleX(84);
    OkButton.Height := ScaleY(28);
    OkButton.Caption := ExpandConstant('{cm:UninstRemoveBtn}');
    OkButton.ModalResult := mrOk;

    CancelButton := TNewButton.Create(Form);
    CancelButton.Parent := Form;
    CancelButton.Left := ScaleX(340);
    CancelButton.Top := ScaleY(130);
    CancelButton.Width := ScaleX(84);
    CancelButton.Height := ScaleY(28);
    CancelButton.Caption := ExpandConstant('{cm:UninstCancelBtn}');
    CancelButton.ModalResult := mrCancel;

    Form.ActiveControl := OkButton;

    if Form.ShowModal() = mrOk then
      RemoveUserData := DataCheck.Checked
    else
      Result := False;   { скасування — деінсталяцію не починаємо }
  finally
    Form.Free();
  end;
end;

{ Реєстровий ключ QSettings("Balachky","Balachky") → HKCU\Software\Balachky.
  Тут і причина початкового багу: раніше ключ (з прапорцем onboarded) НІКОЛИ
  не чистився, тож майстер першого запуску не зʼявлявся після перевстановлення.
  Дані-теку чистить секція [UninstallDelete] за тим самим прапорцем. }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and RemoveUserData then
    RegDeleteKeyIncludingSubkeys(HKEY_CURRENT_USER, 'Software\Balachky');
end;
