#ifndef BuildRoot
#define BuildRoot "..\dist"
#endif
[Setup]
AppId={{F426138A-7ECA-487B-A6CD-47EB31C40462}
AppName=Claude Isolate
AppVersion=1.0.9
AppPublisher=Claude Isolate project
AppPublisherURL=https://github.com/JFT-git/Claude-isolate
DefaultDirName={localappdata}\Programs\Claude Isolate
DefaultGroupName=Claude Isolate
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
UsePreviousAppDir=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.18362
OutputDir={#BuildRoot}
OutputBaseFilename=Claude-isolate-windows-x64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\Claude Isolate.exe
CloseApplications=no
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#BuildRoot}\windows\Claude Isolate\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Claude Isolate"; Filename: "{app}\Claude Isolate.exe"
Name: "{autodesktop}\Claude Isolate"; Filename: "{app}\Claude Isolate.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Claude Isolate.exe"; Description: "{cm:LaunchProgram,Claude Isolate}"; Flags: nowait postinstall skipifsilent

[Code]
const
  // Exit codes of "Claude Isolate Core.exe hypervisor-status|hypervisor-enable"
  // (windows/hypervisor.py).
  HypervisorCanEnable = 10;
  HypervisorRestart = 11;
  HypervisorFirmware = 12;
  // ShellExec: the user declined the administrator prompt.
  ErrorCancelled = 1223;

var
  FreshInstall: Boolean;
  RestartForHypervisor: Boolean;

function PreviousUninstallCommand: String;
var
  Key, Value: String;
begin
  // Same AppId as [Setup]. Per-user installs write the key under HKCU.
  Result := '';
  Key := 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{F426138A-7ECA-487B-A6CD-47EB31C40462}_is1';
  if RegQueryStringValue(HKCU, Key, 'UninstallString', Value) then
    Result := Value
  else if RegQueryStringValue(HKLM, Key, 'UninstallString', Value) then
    Result := Value;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Command, Executable: String;
  Code: Integer;
begin
  // Remove the previous program files before copying the new ones.
  // The uninstaller deletes only the install directory. It does not delete
  // the user profile or a QEMU drive mapping.
  Result := '';
  NeedsRestart := False;
  Command := PreviousUninstallCommand;
  FreshInstall := Command = '';
  if Command = '' then
    Exit;
  Executable := RemoveQuotes(Command);
  if not Exec(Executable, '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART', '', SW_HIDE, ewWaitUntilTerminated, Code) then
  begin
    Result := 'Не удалось запустить удаление предыдущей версии Claude Isolate.';
    Exit;
  end;
  if Code <> 0 then
    Result := 'Не удалось удалить предыдущую версию Claude Isolate. Код ' + IntToStr(Code) + '.';
end;

function GuestData: String;
begin
  Result := ExpandConstant('{param:GUESTDATA|}');
  if Result = '' then
    Result := ExpandConstant('{localappdata}\Claude Isolate');
end;

function CoreExecutable: String;
begin
  Result := ExpandConstant('{app}\Claude Isolate Core.exe');
end;

// Without the Windows Hypervisor Platform QEMU emulates the processor in
// software, several times slower. Turning it on needs administrator rights
// (only this step is elevated) and a restart.
procedure OfferHypervisor;
var
  Code: Integer;
  LogPath: String;
begin
  if not Exec(CoreExecutable, 'hypervisor-status --data "' + GuestData + '"',
    ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Code) then
    Exit;
  Log('Hypervisor status: ' + IntToStr(Code));
  if Code = HypervisorFirmware then
  begin
    if FreshInstall and not WizardSilent then
      MsgBox('Аппаратная виртуализация выключена в настройках BIOS/UEFI (Intel VT-x или AMD-V/SVM) ' +
        'или не поддерживается процессором. Пока она выключена, Linux работает в режиме программной ' +
        'эмуляции, а он в несколько раз медленнее.' + #13#10#13#10 +
        'Если включите её в BIOS/UEFI, установите Claude Isolate ещё раз: установщик настроит Windows.',
        mbInformation, MB_OK);
    Exit;
  end;
  if Code <> HypervisorCanEnable then
    Exit;
  if WizardSilent then
  begin
    if ExpandConstant('{param:ENABLEHYPERVISOR|0}') <> '1' then
      Exit;
  end
  else if MsgBox('Процессор этого компьютера поддерживает аппаратную виртуализацию, но в Windows выключен ' +
    'компонент «Платформа низкоуровневой оболочки Windows». Без него Linux работает в режиме программной ' +
    'эмуляции, а он в несколько раз медленнее.' + #13#10#13#10 +
    'Включить компонент? Windows запросит права администратора, после установки нужно будет ' +
    'перезагрузить компьютер.', mbConfirmation, MB_YESNO) <> IDYES then
    Exit;
  WizardForm.StatusLabel.Caption := 'Enabling Windows Hypervisor Platform / Включение аппаратной виртуализации...';
  LogPath := GuestData + '\hypervisor.log';
  if not ShellExec('runas', CoreExecutable, 'hypervisor-enable --data "' + GuestData + '" --log "' + LogPath + '"',
    ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Code) then
  begin
    Log('Hypervisor enable not started: ' + IntToStr(Code));
    if (Code <> ErrorCancelled) and not WizardSilent then
      MsgBox('Не удалось запросить права администратора: ' + SysErrorMessage(Code), mbError, MB_OK);
    Exit;
  end;
  Log('Hypervisor enable: ' + IntToStr(Code));
  if Code = HypervisorRestart then
    RestartForHypervisor := True
  else if not WizardSilent then
    MsgBox('Не удалось включить аппаратную виртуализацию. Linux будет работать в режиме программной ' +
      'эмуляции. Журнал: ' + LogPath, mbError, MB_OK);
end;

function NeedRestart(): Boolean;
begin
  Result := RestartForHypervisor;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  Parameters: String;
begin
  if CurStep = ssPostInstall then
  begin
    OfferHypervisor;
    WizardForm.StatusLabel.Caption := 'Updating existing Linux environment / Обновление Linux-среды...';
    Parameters := 'upgrade --data "' + GuestData +
      '" --log "' + GuestData + '\installer-update.log"';
    if not Exec(ExpandConstant('{app}\Claude Isolate Core.exe'), Parameters,
      ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
    begin
      Log('Guest update deferred. See installer-update.log. The launcher retries before starting Linux.');
      if not WizardSilent then
        MsgBox('Приложение установлено. Обновление Linux-среды отложено: закройте работающую среду, проверьте подключение и запустите Claude Isolate. Обновление повторится автоматически. Журнал: %LOCALAPPDATA%\Claude Isolate\installer-update.log', mbInformation, MB_OK);
    end;
  end;
end;
