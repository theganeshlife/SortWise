#define AppVersion GetEnv("APP_VERSION")
#define AppBuild GetEnv("APP_BUILD")

[Setup]
AppId={{6E7C2B1A-3F9D-4C1E-9B7A-1D2E3F4A5B6C}
AppName=SortWise
AppVersion={#AppVersion}
AppVerName=SortWise {#AppVersion} (build {#AppBuild})
VersionInfoVersion={#AppVersion}
AppPublisher=Claude and Ganesh Acharya
DefaultDirName={localappdata}\Programs\SortWise
UsePreviousAppDir=no
DefaultGroupName=SortWise
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=SortWiseSetup-{#AppVersion}
SetupIconFile=..\assets\sortwise.ico
UninstallDisplayIcon={app}\SortWise.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=force
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Put a SortWise shortcut on the Desktop"; GroupDescription: "Shortcuts:"
Name: "startup"; Description: "Start SortWise when I sign in, so new files keep getting sorted"; GroupDescription: "Background sorting:"

[Files]
Source: "..\dist\SortWise\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\SortWise"; Filename: "{app}\SortWise.exe"
Name: "{userdesktop}\SortWise"; Filename: "{app}\SortWise.exe"; Tasks: desktopicon
Name: "{userstartup}\SortWise"; Filename: "{app}\SortWise.exe"; Parameters: "--background"; Tasks: startup

[Run]
Filename: "{app}\SortWise.exe"; Description: "Open SortWise now"; Flags: nowait postinstall skipifsilent
Filename: "{app}\SortWise.exe"; Parameters: "--background"; Flags: nowait; Check: WizardSilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM SortWise.exe"; Flags: runhidden; RunOnceId: "StopSortWise"

[UninstallDelete]
Type: files; Name: "{userstartup}\SortWise.lnk"

[InstallDelete]
Type: filesandordirs; Name: "{localappdata}\Programs\Tidy"
Type: files; Name: "{userdesktop}\Tidy.lnk"
Type: files; Name: "{userstartup}\Tidy.lnk"
Type: filesandordirs; Name: "{userprograms}\Tidy"

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM Tidy.exe', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM SortWise.exe', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Result := '';
end;
