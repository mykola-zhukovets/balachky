; Інсталер «Балачки у Коростені» — per-user, БЕЗ UAC (PrivilegesRequired=lowest).
; Збірка:  ISCC.exe installer\balachky.iss   (спершу pyinstaller balachky.spec)
; Результат: installer\Output\BalachkySetup-<версія>.exe

#include "version.iss"
#include "farewell-data.iss"

#define AppExeName "Balachky.exe"

[Setup]
; AppId — НЕ ЗМІНЮВАТИ НІКОЛИ: за цим GUID Windows та Inno впізнають
; встановлену програму (оновлення поверх, коректне видалення).
AppId={{2C5BBCE3-5047-47A6-96B0-C78B12E059F9}
AppName={cm:AppDisplayName}
AppVersion={#AppVersion}
AppVerName={cm:AppDisplayName} {#AppVersion}
AppPublisher=Mykola Zhukovets
VersionInfoVersion={#WindowsFileVersion}
VersionInfoCompany=Mykola Zhukovets
VersionInfoDescription=Balachky Setup
VersionInfoProductName=Balachky
VersionInfoProductVersion={#WindowsFileVersion}
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
; Restart Manager: закрити зайнятий EXE перед оновленням і повернути процес,
; якщо його закрив саме інсталятор.
CloseApplications=yes
RestartApplications=yes

[Languages]
Name: "ukrainian"; MessagesFile: "compiler:Languages\Ukrainian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

; Тексти кастомного вікна деінсталятора — двомовні (обирається за ActiveLanguage).
[CustomMessages]
ukrainian.AppDisplayName=Балачки у Коростені
english.AppDisplayName=Balachky
ukrainian.UninstTitle=Балачки у Коростені — видалення
english.UninstTitle=Uninstall Balachky
ukrainian.UninstPrompt=Програму буде видалено. Папка %LOCALAPPDATA%\Balachky типово залишиться на комп’ютері й буде доступна після повторного встановлення. Дані у власних папках і спільний кеш моделей цей деінсталятор не видаляє.
english.UninstPrompt=Balachky will be uninstalled. The %LOCALAPPDATA%\Balachky folder stays on this computer by default and will be available if you reinstall the app. Data in custom folders and the shared model cache are not deleted by this uninstaller.
ukrainian.UninstRemoveData=Також назавжди видалити папку даних Балачок: налаштування, словники, історію, записи, розшифровки та завантажені компоненти
english.UninstRemoveData=Also permanently delete the Balachky data folder: settings, dictionaries, history, recordings, transcripts, and downloaded components
ukrainian.UninstFarewellPrompt=Перед видаленням Ви можете залишити відгук, переглянути реквізити підтримки автора або одразу видалити програму. Усі дії — за бажанням.
english.UninstFarewellPrompt=Before uninstalling, you can leave feedback, view the author’s support details, or uninstall the app right away. Nothing here is required.
ukrainian.UninstFarewellFeedbackBtn=Щось не працювало / чогось бракувало
english.UninstFarewellFeedbackBtn=Something did not work / something was missing
ukrainian.UninstFarewellSupportBtn=Було корисно
english.UninstFarewellSupportBtn=It was useful
ukrainian.UninstRemoveBtn=Просто видалити
english.UninstRemoveBtn=Just uninstall
ukrainian.UninstCancelBtn=Скасувати
english.UninstCancelBtn=Cancel
ukrainian.UninstSupportTitle=Підтримка автора
english.UninstSupportTitle=Support the author
ukrainian.UninstSupportPrompt=Це ті самі реквізити, що й у розділі “Підтримка автора” в Налаштуваннях. Ви можете скопіювати потрібний рядок.
english.UninstSupportPrompt=These are the same details shown under “Support the author” in Settings. You can copy the line you need.
ukrainian.UninstSupportMono=Monobank (гривня)
english.UninstSupportMono=Monobank (UAH)
ukrainian.UninstSupportPrivatUsd=PrivatBank (долари)
english.UninstSupportPrivatUsd=PrivatBank (USD)
ukrainian.UninstSupportPrivatEur=PrivatBank (євро)
english.UninstSupportPrivatEur=PrivatBank (EUR)
ukrainian.UninstSupportUsdt=USDT (TRC-20)
english.UninstSupportUsdt=USDT (TRC-20)
ukrainian.UninstSupportBtc=Bitcoin
english.UninstSupportBtc=Bitcoin
ukrainian.UninstSupportEth=Ethereum
english.UninstSupportEth=Ethereum
ukrainian.UninstSupportBackBtn=Повернутися
english.UninstSupportBackBtn=Back
ukrainian.UninstFeedbackError=Не вдалося відкрити форму відгуку в браузері.
english.UninstFeedbackError=Could not open the feedback form in your browser.
ukrainian.UninstCleanRegistryPrompt=Прибрати з реєстру Windows збережені налаштування програми (гілка HKCU\Software\Balachky: розмір і позиція вікна, підказки, стан майстра першого запуску, кеш перевірки оновлень)? Це не стосується записів, розшифровок, словників і завантажених моделей — вони, якщо є, лежать окремо в %LOCALAPPDATA%\Balachky і цим не видаляються.
english.UninstCleanRegistryPrompt=Remove the saved app settings from the Windows registry (key HKCU\Software\Balachky: window size and position, hint flags, first-run wizard state, update-check cache)? This does not affect your recordings, transcripts, dictionaries, or downloaded models — if present, they live separately in %LOCALAPPDATA%\Balachky and are not deleted by this.

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
Name: "{autoprograms}\{cm:AppDisplayName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{cm:AppDisplayName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{cm:AppDisplayName}}"; Flags: nowait postinstall skipifsilent

; Користувацькі дані (%LOCALAPPDATA%\Balachky: config.toml, profiles\) інсталер
; типово НЕ чіпає — вони переживають перевстановлення і видалення програми
; свідомо. Видалення відбувається ЛИШЕ якщо користувач у деінсталяторі свідомо
; позначив «Видалити також налаштування і локальні дані» (див. [Code]).
[UninstallDelete]
Type: filesandordirs; Name: "{localappdata}\Balachky"; Check: RemoveUserDataChecked
Type: filesandordirs; Name: "{%TEMP}\balachky-meeting-*"
Type: filesandordirs; Name: "{%TEMP}\balachky-meeting-media-*"
Type: filesandordirs; Name: "{%TEMP}\balachky-tts-plain-*"

[Code]
{ «Я приймаю умови ліцензійної угоди» позначено одразу — стандарт Inno
  («Я НЕ приймаю») означав зайвий клік для кожного, хто встановлює програму.
  Користувач і далі може перемкнути на «Я не приймаю» та скасувати встановлення. }
procedure InitializeWizard();
begin
  WizardForm.LicenseAcceptedRadio.Checked := True;
end;

{ Прапорець вибору користувача в деінсталяторі; типово ВИМКНЕНО. }
var
  RemoveUserData: Boolean;

function RemoveUserDataChecked(): Boolean;
begin
  Result := RemoveUserData;
end;

procedure OpenUninstallFeedback(Sender: TObject);
var
  ErrorCode: Integer;
begin
  if not ShellExec('open', '{#FarewellIssueUrl}', '', '', SW_SHOWNORMAL,
      ewNoWait, ErrorCode) then
    MsgBox(
      ExpandConstant('{cm:UninstFeedbackError}'), mbError, MB_OK);
end;

procedure ShowUninstallSupport(Sender: TObject);
var
  Form: TSetupForm;
  Intro: TNewStaticText;
  Details: TNewMemo;
  BackButton: TNewButton;
begin
  Form := CreateCustomForm(ScaleX(640), ScaleY(370), False, True);
  try
    Form.Caption := ExpandConstant('{cm:UninstSupportTitle}');
    Form.Position := poScreenCenter;

    Intro := TNewStaticText.Create(Form);
    Intro.Parent := Form;
    Intro.Left := ScaleX(16);
    Intro.Top := ScaleY(16);
    Intro.Width := ScaleX(608);
    Intro.Height := ScaleY(48);
    Intro.AutoSize := False;
    Intro.WordWrap := True;
    Intro.Caption := ExpandConstant('{cm:UninstSupportPrompt}');

    Details := TNewMemo.Create(Form);
    Details.Parent := Form;
    Details.Left := ScaleX(16);
    Details.Top := ScaleY(72);
    Details.Width := ScaleX(608);
    Details.Height := ScaleY(240);
    Details.ReadOnly := True;
    Details.ScrollBars := ssBoth;
    Details.WordWrap := False;
    Details.Text :=
      ExpandConstant('{cm:UninstSupportMono}') + ': {#FarewellSupportMonoUah}' + #13#10 +
      ExpandConstant('{cm:UninstSupportPrivatUsd}') + ': {#FarewellSupportPrivatUsd}' + #13#10 +
      ExpandConstant('{cm:UninstSupportPrivatEur}') + ': {#FarewellSupportPrivatEur}' + #13#10 +
      ExpandConstant('{cm:UninstSupportUsdt}') + ': {#FarewellSupportUsdtTrc20}' + #13#10 +
      ExpandConstant('{cm:UninstSupportBtc}') + ': {#FarewellSupportBtc}' + #13#10 +
      ExpandConstant('{cm:UninstSupportEth}') + ': {#FarewellSupportEth}';

    BackButton := TNewButton.Create(Form);
    BackButton.Parent := Form;
    BackButton.Left := ScaleX(524);
    BackButton.Top := ScaleY(326);
    BackButton.Width := ScaleX(100);
    BackButton.Height := ScaleY(28);
    BackButton.Caption := ExpandConstant('{cm:UninstSupportBackBtn}');
    BackButton.ModalResult := mrOk;
    Form.ActiveControl := BackButton;

    Form.ShowModal();
  finally
    Form.Free();
  end;
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
  Lbl, FarewellLbl: TNewStaticText;
  DataCheck: TNewCheckBox;
  FarewellFeedbackButton, FarewellSupportButton: TNewButton;
  FarewellUninstallButton, CancelButton: TNewButton;
begin
  RemoveUserData := False;
  Result := True;

  if UninstallSilent() then
    Exit;   { тихий режим: без форми, дані зберігаємо, продовжуємо видалення }

  Form := CreateCustomForm(ScaleX(840), ScaleY(365), False, True);
  try
    Form.Caption := ExpandConstant('{cm:UninstTitle}');
    Form.Position := poScreenCenter;

    Lbl := TNewStaticText.Create(Form);
    Lbl.Parent := Form;
    Lbl.Left := ScaleX(16);
    Lbl.Top := ScaleY(16);
    Lbl.Width := ScaleX(808);
    Lbl.AutoSize := False;
    Lbl.Height := ScaleY(78);
    Lbl.WordWrap := True;
    Lbl.Caption := ExpandConstant('{cm:UninstPrompt}');

    FarewellLbl := TNewStaticText.Create(Form);
    FarewellLbl.Parent := Form;
    FarewellLbl.Left := ScaleX(16);
    FarewellLbl.Top := ScaleY(100);
    FarewellLbl.Width := ScaleX(808);
    FarewellLbl.Height := ScaleY(48);
    FarewellLbl.AutoSize := False;
    FarewellLbl.WordWrap := True;
    FarewellLbl.Caption := ExpandConstant('{cm:UninstFarewellPrompt}');

    FarewellFeedbackButton := TNewButton.Create(Form);
    FarewellFeedbackButton.Parent := Form;
    FarewellFeedbackButton.Left := ScaleX(16);
    FarewellFeedbackButton.Top := ScaleY(164);
    FarewellFeedbackButton.Width := ScaleX(256);
    FarewellFeedbackButton.Height := ScaleY(52);
    FarewellFeedbackButton.Caption :=
      ExpandConstant('{cm:UninstFarewellFeedbackBtn}');
    FarewellFeedbackButton.OnClick := @OpenUninstallFeedback;

    FarewellSupportButton := TNewButton.Create(Form);
    FarewellSupportButton.Parent := Form;
    FarewellSupportButton.Left := ScaleX(292);
    FarewellSupportButton.Top := ScaleY(164);
    FarewellSupportButton.Width := ScaleX(256);
    FarewellSupportButton.Height := ScaleY(52);
    FarewellSupportButton.Caption :=
      ExpandConstant('{cm:UninstFarewellSupportBtn}');
    FarewellSupportButton.OnClick := @ShowUninstallSupport;

    FarewellUninstallButton := TNewButton.Create(Form);
    FarewellUninstallButton.Parent := Form;
    FarewellUninstallButton.Left := ScaleX(568);
    FarewellUninstallButton.Top := ScaleY(164);
    FarewellUninstallButton.Width := ScaleX(256);
    FarewellUninstallButton.Height := ScaleY(52);
    FarewellUninstallButton.Caption := ExpandConstant('{cm:UninstRemoveBtn}');
    FarewellUninstallButton.ModalResult := mrOk;

    DataCheck := TNewCheckBox.Create(Form);
    DataCheck.Parent := Form;
    DataCheck.Left := ScaleX(16);
    DataCheck.Top := ScaleY(232);
    DataCheck.Width := ScaleX(808);
    DataCheck.Height := ScaleY(70);
    DataCheck.Caption := ExpandConstant('{cm:UninstRemoveData}');
    DataCheck.Checked := False;

    CancelButton := TNewButton.Create(Form);
    CancelButton.Parent := Form;
    CancelButton.Left := ScaleX(724);
    CancelButton.Top := ScaleY(321);
    CancelButton.Width := ScaleX(100);
    CancelButton.Height := ScaleY(28);
    CancelButton.Caption := ExpandConstant('{cm:UninstCancelBtn}');
    CancelButton.ModalResult := mrCancel;

    Form.ActiveControl := FarewellUninstallButton;

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

  { Користувач НЕ обирав повне видалення даних вище (типовий випадок —
    «Просто видалити»), тож реєстровий ключ і далі лежав би на диску вічно
    (підтверджено живим тестом). Питаємо окремо, наприкінці видалення, лише
    про сам реєстровий ключ налаштувань — не про папку даних. Тихий режим
    (/SILENT, /VERYSILENT) MsgBox не показує: питання пропускаємо, ключ не
    чіпаємо (безпечний дефолт). }
  if (CurUninstallStep = usPostUninstall) and (not RemoveUserData)
      and (not UninstallSilent())
      and RegKeyExists(HKEY_CURRENT_USER, 'Software\Balachky') then
    if MsgBox(ExpandConstant('{cm:UninstCleanRegistryPrompt}'),
        mbConfirmation, MB_YESNO) = IDYES then
      RegDeleteKeyIncludingSubkeys(HKEY_CURRENT_USER, 'Software\Balachky');
end;
