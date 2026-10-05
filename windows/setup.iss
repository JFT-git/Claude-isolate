[Setup]
AppId={{F426138A-7ECA-487B-A6CD-47EB31C40462}
AppName=Claude Isolate
AppVersion=0.3.6
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
CloseApplications=yes

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
