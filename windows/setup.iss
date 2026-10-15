#ifndef BuildRoot
#define BuildRoot "..\dist"
#endif
[Setup]
AppId={{F426138A-7ECA-487B-A6CD-47EB31C40462}
AppName=Claude Isolate
AppVersion=1.0.8
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

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  Parameters: String;
begin
  if CurStep = ssPostInstall then
  begin
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
