; Inno Setup script - builds RemoteDesk-Setup.exe (Hebrew installer)
#define AppVersion GetEnv("APP_VERSION")
#if AppVersion == ""
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{6F1E2B7A-5C3D-4E8F-9A1B-2C3D4E5F6A7B}
AppName=RemoteDesk
AppVersion={#AppVersion}
AppPublisher=RemoteDesk
DefaultDirName={autopf}\RemoteDesk
DefaultGroupName=RemoteDesk
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=RemoteDesk-Setup
SetupIconFile=..\icon.ico
UninstallDisplayIcon={app}\RemoteDesk.exe
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Languages]
Name: "hebrew"; MessagesFile: "compiler:Languages\Hebrew.isl"

[Tasks]
Name: "service"; Description: "התקן כשירות מערכת - גישה מלאה (מסך כניסה, UAC, גם כשהתוכנה סגורה)"; Flags: checkedonce
Name: "desktopicon"; Description: "צור קיצור דרך בשולחן העבודה"

[Files]
Source: "..\dist\RemoteDesk.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\RemoteDesk"; Filename: "{app}\RemoteDesk.exe"
Name: "{autodesktop}\RemoteDesk"; Filename: "{app}\RemoteDesk.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\RemoteDesk.exe"; Parameters: "--install-service"; StatusMsg: "מתקין את שירות המערכת..."; Flags: runhidden waituntilterminated; Tasks: service
Filename: "{app}\RemoteDesk.exe"; Description: "פתח את RemoteDesk"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\RemoteDesk.exe"; Parameters: "--uninstall-service"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveService"
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM RemoteDesk.exe /T"; Flags: runhidden waituntilterminated; RunOnceId: "KillApp"

[UninstallDelete]
Type: filesandordirs; Name: "{commonappdata}\RemoteDesk"

[Code]
{ Stop the running service, agent and window before files are replaced -
  otherwise Windows refuses to overwrite RemoteDesk.exe ("Access is denied"). }
procedure StopRemoteDesk();
var
  R: Integer;
begin
  Exec(ExpandConstant('{sys}\sc.exe'), 'stop RemoteDesk', '', SW_HIDE, ewWaitUntilTerminated, R);
  Sleep(2500);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM RemoteDesk.exe /T', '', SW_HIDE, ewWaitUntilTerminated, R);
  Sleep(1000);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopRemoteDesk();
  Result := '';
end;
