[Setup]
AppId={{F426138A-7ECA-487B-A6CD-47EB31C40462}
AppName=Claude Isolate
AppVersion=0.3.13
AppPublisher=Claude Isolate project
AppPublisherURL=https://github.com/JFT-git/Claude-isolate
DefaultDirName={localappdata}\Programs\Claude Isolate
DefaultGroupName=Claude Isolate
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.18362
OutputDir=..\dist
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
Source: "..\dist\windows\Claude Isolate\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Claude Isolate"; Filename: "{app}\Claude Isolate.exe"
Name: "{autodesktop}\Claude Isolate"; Filename: "{app}\Claude Isolate.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Claude Isolate.exe"; Description: "{cm:LaunchProgram,Claude Isolate}"; Flags: nowait postinstall skipifsilent

[Code]
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
